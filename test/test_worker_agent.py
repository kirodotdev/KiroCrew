"""The ``kirocrew-worker`` agent spec, and the conductor side of the same server.

Pins the Phase 2 exit criteria about specs: the worker spec is the default agent's
SUPERSET plus ``@kirocrew-work``, and the conductor specs that mount the server
auto-approve the two conductor tools while auto-approving neither worker tool.

Which conductors those are is the other half. ``kirocrew-conductor`` mounts it —
it IS the ledger conductor — and so does its deprecated ``kirocrew-ledger-conductor``
alias, which emits the same spec under the old name for one release.
``kirocrew-pipeline-conductor`` does not: its children report through the
``pipeline-conductor`` skill's own scripts, so the mount would grant a flow whose
procedure it does not run.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from source_corpus import parsed_candidates, source_texts

from kiro_crew import agent
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    WORKER_AGENT_FILENAME,
)

# One xdist worker for the whole module: the four enumeration gates below share ONE read of
# src/ (~1,550 files, 0.9 s and ~187 MB of text while it is warm), and under `--dist
# loadgroup` an unmarked module is spread across workers -- so those four can land on four
# workers, each paying the read again and each holding its own copy of the corpus at the
# same time. Grouping keeps it single-copy per run; the copy itself is released at module
# teardown by `conftest._release_source_corpus_after_module`.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_worker_agent")


def _package_sources() -> tuple[tuple[Path, str], ...]:
    """Every ``kiro_crew`` module except ``agent.py``, off the shared corpus read.

    Four enumeration tests below reason over the package, and one traversal serves all of
    them. A walk per test is thousands of small reads each -- seconds on Linux and far
    worse on the Windows shards, whose job budget is 40 minutes for a quarter of a
    100k-test suite. The rule-shaped assertions are what matter; repeating the traversal
    is not part of them.

    The sharing is ``test/source_corpus.py``'s and NOT an ``lru_cache`` of our own,
    because the text of the ~1,550 modules under ``src/`` is ~115 MB of retained ``str``
    and only the corpus helper's copy can be released: ``test/conftest.py``'s
    ``_release_source_corpus_after_module`` calls ``_clear_caches()`` at module teardown,
    but a second tuple of ours holding those same ``str`` objects would keep every one of
    them alive for the rest of the xdist worker's life, paid by every later test that
    worker runs. So this stays uncached -- rebuilding a tuple of ~1,550 references costs
    nothing measurable -- and the ``agent.py`` exclusion stays HERE with the gates rather
    than in the shared helper, because which files a gate polices is that gate's contract
    (and ``agent.py``, defining every name these gates hunt for, would match them all).
    """
    return tuple((path, text) for path, text in source_texts() if path.name != "agent.py")


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


def test_the_worker_prompt_does_not_carry_the_retired_verbosity_token(specs):
    """Reply style arrives as session-context chrome for every agent; a token
    left here would reach the model as a literal."""
    assert "{{VERBOSITY_BLOCK}}" not in specs[WORKER_AGENT_FILENAME]["prompt"]


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


@pytest.mark.parametrize("filename", [CONDUCTOR_AGENT_FILENAME, LEDGER_CONDUCTOR_AGENT_FILENAME])
def test_a_ledger_conductor_mounts_the_server_and_grants_only_its_own_half(specs, filename):
    """Per tool rather than whole-server, because the worker half is mounted on the
    same server. Missing these is not an error but a silent approval prompt on every
    patrol cycle, which is why they are asserted.

    Parametrized over the live spec AND its deprecated alias: a session still
    running under the old name pays the same approval costs, so the alias losing a
    grant would strand it on a prompt nobody is there to answer."""
    spec = specs[filename]
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


def test_a_conductor_with_another_procedure_does_not_mount_the_server(specs):
    """The pipeline conductor mounted this server briefly, and the mount is retracted.

    The tools alone do not describe the procedure they came with: the ledger flow
    binds before it seeds and reads a record instead of a transcript, so mounting
    them on an agent that ships a different procedure hands its users a procedure
    they did not choose. Asserted negatively, on every surface a mount can survive
    on, so it cannot return unnoticed — the KAS rule especially, since nothing
    reads ``allowedTools`` on that backend.
    """
    spec = specs[PIPELINE_CONDUCTOR_AGENT_FILENAME]
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


# ── parity with the DEFAULT SPEC ON DISK, not with the template ────────────


#: A default agent spec shaped the way a real one is after a user has used the
#: product: servers an app registration and the dashboard mounted, whole-server
#: grants for them, and a model pinned through ``config.json``. None of it reaches
#: ``build_agent_config``, which composes the shipped template with the override
#: file only — which is exactly why the worker mirrors the FILE.
_DEFAULT_SPEC_ON_DISK: dict[str, Any] = {
    "name": "kirocrew",
    "model": "claude-opus-5",
    "tools": [
        "execute_bash",
        "fs_read",
        "fs_write",
        "@kirocrew-core",
        "@kirocrew-cron",
        "@kirocrew-dashboard",
        "@builder-mcp",
        "@pdf",
    ],
    "allowedTools": [
        "fs_read",
        "@kirocrew-cron",
        "@kirocrew-dashboard/session_create",
        "@builder-mcp",
        "@pdf",
    ],
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]},
        "kirocrew-cron": {"command": "kirocrew", "args": ["mcp-cron"]},
        "kirocrew-dashboard": {"command": "kirocrew", "args": ["mcp-dashboard"]},
        "builder-mcp": {"command": "builder-mcp", "args": []},
        "pdf": {"command": "uvx", "args": ["pdf-mcp"]},
    },
}


@pytest.fixture()
def worker_from_installed_default(tmp_path, monkeypatch):
    """Install the worker beside a used-looking default spec, and read it back."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")

    def _install() -> dict[str, Any]:
        agent._install_worker_agent()
        return json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    return _install


def test_the_worker_mirrors_the_servers_the_user_mounted_on_the_default(
    worker_from_installed_default,
):
    """The gap this closes. A user's servers live in ``kirocrew.json``, not in the
    template, so a worker built from the template alone is dispatched without the
    tools its dispatcher holds — and a work item that needs one has no way to ask."""
    worker = worker_from_installed_default()
    for name, entry in _DEFAULT_SPEC_ON_DISK["mcpServers"].items():
        if name in agent._worker_unassignable_servers():
            continue  # asserted absent by the opt-in tests below
        assert worker["mcpServers"][name] == entry, name
    for ref in ("@builder-mcp", "@pdf"):
        assert ref in worker["tools"], ref
        assert ref in worker["allowedTools"], ref


def test_the_worker_mirrors_the_default_model(worker_from_installed_default):
    """A dispatched worker does the same work under the same model. Left on the
    shipped sentinel it silently runs a weaker one than the session that dispatched
    it, on work the dispatcher sized against its own."""
    assert worker_from_installed_default()["model"] == "claude-opus-5"


def test_the_worker_keeps_a_model_the_user_pinned_on_the_worker_itself(
    worker_from_installed_default, tmp_path, monkeypatch
):
    """The one case where mirroring is wrong, and it is read off the sidecar the
    dashboard's model PATCH already writes rather than guessed from the spec."""
    worker_from_installed_default()
    stale = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    stale["model"] = "claude-haiku-4.5"
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(
        agent.agent_state,
        "get_model_managed",
        lambda name, strict=False: False if name == "kirocrew-worker" else None,
    )
    assert worker_from_installed_default()["model"] == "claude-haiku-4.5"


@pytest.mark.parametrize("bad", [123, None, ["claude-x"], {}, "", "   ", True, 1.5])
def test_a_model_pin_that_is_not_a_string_loses_to_the_mirrored_default(
    bad, worker_from_installed_default, tmp_path, monkeypatch, caplog
):
    """The pin is the ONE field carried across from the previous worker file, and it
    arrives from the dashboard's model PATCH and from the file itself -- neither of which
    guarantees a string. kiro-cli validates the spec strictly, so copying a number, a
    list or a null through writes a worker that cannot load AT ALL, which turns a
    cosmetic bad pin into a worker that will not start."""
    worker_from_installed_default()
    spec = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    spec["model"] = bad
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(
        agent.agent_state,
        "get_model_managed",
        lambda name, strict=False: False if name == "kirocrew-worker" else None,
    )

    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        written = worker_from_installed_default()

    # The recoverable direction: the dispatcher's own model, never an unloadable spec.
    assert written["model"] == "claude-opus-5"
    assert isinstance(written["model"], str)

    warnings = [r.getMessage() for r in caplog.records if "model" in r.getMessage()]
    assert warnings, f"a discarded pin must be reported: {[r.getMessage() for r in caplog.records]}"
    # The KEY and the offending TYPE, and not the value -- a malformed model field is a
    # shape problem, and the field can hold whatever a PATCH put there.
    assert "'model'" in warnings[0]
    assert type(bad).__name__ in warnings[0]
    if bad not in (None, "", "   ") and not isinstance(bad, bool):
        assert str(bad) not in warnings[0], warnings[0]


def test_a_string_model_pin_is_still_preserved_verbatim(
    worker_from_installed_default, tmp_path, monkeypatch, caplog
):
    """The type check must not cost the feature it guards: a real pick is carried across
    unchanged and says nothing, because a valid pin is not an event."""
    worker_from_installed_default()
    spec = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    spec["model"] = "claude-x"
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(
        agent.agent_state,
        "get_model_managed",
        lambda name, strict=False: False if name == "kirocrew-worker" else None,
    )

    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        written = worker_from_installed_default()

    assert written["model"] == "claude-x"
    assert not [r for r in caplog.records if "not a non-empty str" in r.getMessage()]


def test_the_worker_kas_policy_covers_the_mirrored_servers(worker_from_installed_default):
    """``allowedTools`` is not read on the KAS backend, so a mirrored grant with no
    rule beside it is a grant that does nothing there. ``kirocrew-cron`` is asserted
    per verb rather than whole because scheduling is subtracted from it — see
    ``test_the_kas_policy_carries_no_scheduling_match``."""
    rendered = json.dumps(worker_from_installed_default()["permissions"])
    for server in ("builder-mcp", "pdf"):
        assert f"{server}/*" in rendered, server
    assert "kirocrew-cron/cron_list" in rendered


def test_the_work_server_survives_the_mirror(worker_from_installed_default):
    """The mirror replaces ``mcpServers`` wholesale, and the default never carries
    this server — an opt-in set belongs to the agents whose spec names it."""
    worker = worker_from_installed_default()
    assert worker["mcpServers"]["kirocrew-work"]["args"][-1] == "mcp-work"
    assert "@kirocrew-work" in worker["tools"]
    assert "@kirocrew-work/work_brief" in worker["allowedTools"]
    assert "@kirocrew-work/work_report" in worker["allowedTools"]


# ── the refresh heals a spec an older build wrote ──────────────────────────


def _stale_worker_spec() -> dict[str, Any]:
    """A worker spec as an older build left it: template-only, and out of date."""
    return {
        "name": "kirocrew-worker",
        "description": "an older build's description",
        "prompt": "an older build's prompt",
        "model": "auto",
        "tools": ["fs_read", "@kirocrew-core", "@kirocrew-work"],
        "allowedTools": ["fs_read", "@kirocrew-work/work_brief"],
        "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}},
    }


def test_a_refresh_heals_a_stale_worker_spec(worker_from_installed_default, tmp_path):
    """Existing installs must not need a hand-edit. The installer runs on every
    ``rebuild_agent_config``, so the missing servers, grants and model arrive on the
    next gateway start."""
    (tmp_path / WORKER_AGENT_FILENAME).write_text(
        json.dumps(_stale_worker_spec()), encoding="utf-8"
    )
    healed = worker_from_installed_default()
    assert healed["model"] == "claude-opus-5"
    assert "builder-mcp" in healed["mcpServers"]
    assert "@builder-mcp" in healed["allowedTools"]
    assert "@pdf" in healed["tools"]


def test_a_refresh_restores_the_worker_prompt_and_description(
    worker_from_installed_default, tmp_path
):
    """The worker's identity is machine-owned: the prompt IS the reporting contract,
    so a spec written before a revision to it has to be brought forward."""
    (tmp_path / WORKER_AGENT_FILENAME).write_text(
        json.dumps(_stale_worker_spec()), encoding="utf-8"
    )
    healed = worker_from_installed_default()
    assert healed["prompt"] == agent._WORKER_SYSTEM_PROMPT
    assert "reports status against that item" in healed["description"]
    assert healed["name"] == "kirocrew-worker"


def test_a_refresh_carries_nothing_but_a_frozen_model_off_the_previous_file(
    worker_from_installed_default, tmp_path
):
    """The worker file is DERIVED, so an entry in it is either a copy of the default's
    or the user's own and nothing on disk says which. An add-only merge therefore
    resurrects a server the default has since dropped — register an app, deregister
    it, and its tools stay callable on the worker for good — and re-admits an
    ``autoApprove`` no ceiling has seen. So the spec is a function of the default plus
    the work server, and the one field that crosses is a frozen ``model``."""
    stale = _stale_worker_spec()
    stale["tools"].append("@a-deregistered-app")
    stale["allowedTools"].append("@a-deregistered-app")
    stale["mcpServers"]["a-deregistered-app"] = {"command": "gone", "args": ["--stdio"]}
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(stale), encoding="utf-8")

    healed = worker_from_installed_default()
    assert "@a-deregistered-app" not in healed["tools"]
    assert "@a-deregistered-app" not in healed["allowedTools"]
    assert "a-deregistered-app" not in healed["mcpServers"]
    assert "a-deregistered-app" not in json.dumps(healed["permissions"])


def test_a_mirrored_grant_still_faces_the_ceiling(
    worker_from_installed_default, tmp_path, monkeypatch
):
    """The mirror reads a file the ceiling never filtered on the way in, so a
    tightened ceiling must still win: the filter runs after every source is folded in."""
    (tmp_path / WORKER_AGENT_FILENAME).write_text(
        json.dumps(_stale_worker_spec()), encoding="utf-8"
    )
    monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: False)

    healed = worker_from_installed_default()
    assert healed["allowedTools"] == []
    assert healed["permissions"] == {"rules": []}
    # Still MOUNTED — the ceiling removes auto-approve, not the tool.
    assert "@kirocrew-work" in healed["tools"]


def test_the_mirror_is_authoritative_over_a_stale_same_named_entry(
    worker_from_installed_default, tmp_path
):
    """A server the previous worker file holds under a name the default also holds
    resolves to the DEFAULT's entry: the mirror is the authority for anything the
    default carries, and a stale copy of it is never the tie-breaker."""
    stale = _stale_worker_spec()
    stale["mcpServers"]["builder-mcp"] = {"command": "an-old-path", "args": []}
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(stale), encoding="utf-8")
    healed = worker_from_installed_default()
    assert healed["mcpServers"]["builder-mcp"] == {"command": "builder-mcp", "args": []}


def test_the_mirrored_keys_are_the_parity_surface_and_omit_permissions():
    """One derivation home. ``permissions`` is derived from the mirrored grants, so
    listing it here would restate a value out of a file the ceiling never filtered.
    ``excludedTools`` IS here: it is a restriction, and mirroring grants without it
    would make the worker more permissive than the agent it mirrors."""
    assert agent._WORKER_MIRRORED_KEYS == (
        "tools",
        "allowedTools",
        "excludedTools",
        "mcpServers",
        "model",
    )
    assert "permissions" not in agent._WORKER_MIRRORED_KEYS
    # Derived from the shape map, so the two cannot disagree on the surface.
    assert agent._WORKER_MIRRORED_KEYS == tuple(agent._WORKER_MIRRORED_SHAPES)


def test_the_worker_falls_back_to_the_template_with_no_default_spec_on_disk(tmp_path, monkeypatch):
    """A fresh install writes the worker beside a default that may not exist yet, and
    the template is the only base there is. Nothing may raise on that path — a
    failed install disables one feature, and this one is the dispatch target."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    template = agent.build_agent_config()
    assert set(template["tools"]) <= set(worker["tools"])
    assert "@kirocrew-work" in worker["tools"]


# ── the one subtraction: cron scheduling ──────────────────────────────────


def test_the_worker_never_gains_cron_add_from_a_whole_server_grant(
    worker_from_installed_default,
):
    """The default carries ``@kirocrew-cron`` whole once a rebuild has widened it, and
    a whole-server grant covers ``cron_add`` too. Mirroring it verbatim would grant
    scheduling by the back door while the exclusion list read as honoured."""
    worker = worker_from_installed_default()
    assert "@kirocrew-cron" in _DEFAULT_SPEC_ON_DISK["allowedTools"]  # noqa: E501 - fixture pin
    assert "@kirocrew-cron" not in worker["allowedTools"]
    for ref in agent._WORKER_EXCLUDED_GRANTS:
        assert ref not in worker["allowedTools"], ref


def test_the_narrowed_cron_surface_survives_the_substitution(worker_from_installed_default):
    """Subtracting scheduling must not cost the worker the verbs it holds today: acting
    on a job that already exists is within one item's reach."""
    allowed = worker_from_installed_default()["allowedTools"]
    for verb in (
        "cron_list",
        "cron_pause",
        "cron_resume",
        "cron_trigger",
        "cron_remove",
        "cron_remove_all",
    ):
        assert f"@kirocrew-cron/{verb}" in allowed, verb


def test_an_explicit_cron_add_grant_on_the_default_is_dropped(tmp_path, monkeypatch):
    """The other shape the exclusion meets — an exact ref rather than a whole server."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = dict(_DEFAULT_SPEC_ON_DISK)
    spec["allowedTools"] = ["fs_read", "@kirocrew-cron/cron_list", "@kirocrew-cron/cron_add"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "@kirocrew-cron/cron_list" in worker["allowedTools"]
    assert "@kirocrew-cron/cron_add" not in worker["allowedTools"]


def test_a_cron_add_grant_in_the_previous_worker_file_does_not_survive(
    worker_from_installed_default, tmp_path
):
    """Two independent reasons it cannot come back, and the test does not care which
    one fires: the file contributes no grants at all, and the exclusion would drop it
    anyway."""
    stale = _stale_worker_spec()
    stale["allowedTools"].append("@kirocrew-cron/cron_add")
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(stale), encoding="utf-8")
    assert "@kirocrew-cron/cron_add" not in worker_from_installed_default()["allowedTools"]


def test_the_excluded_cron_verbs_stay_mounted(worker_from_installed_default):
    """AUTO-APPROVE is withheld, not the tool. An item that genuinely needs a schedule
    can still ask a human through the approval gate rather than failing silently."""
    worker = worker_from_installed_default()
    assert "@kirocrew-cron" in worker["tools"]
    assert "kirocrew-cron" in worker["mcpServers"]


def test_the_kas_policy_carries_no_scheduling_match(worker_from_installed_default):
    """``allowedTools`` is not read on the KAS backend, so a subtraction that stopped
    at the grant list would leave scheduling auto-approved there."""
    rules = json.dumps(worker_from_installed_default()["permissions"])
    assert "kirocrew-cron/*" not in rules
    assert "cron_add" not in rules
    assert "cron_list" in rules


def test_the_exclusion_set_is_pinned_and_disjoint_from_the_template_grants():
    """Named, so the reason is in one place. Disjoint, because an exclusion the
    template also auto-approves would be a rule contradicting itself — and the
    substitution above draws the worker's cron surface FROM those template grants."""
    assert agent._WORKER_EXCLUDED_GRANTS == frozenset(
        {
            "@kirocrew-cron/cron_add",
            "@kirocrew-cron/cron_update",
            "@kirocrew-cron/cron_secret_request",
        }
    )
    template = set(agent.build_agent_config().get("allowedTools") or [])
    assert not (template & agent._WORKER_EXCLUDED_GRANTS)


def test_a_whole_server_grant_with_no_template_grants_fails_closed(tmp_path, monkeypatch):
    """The substitution's failure direction. With nothing to substitute the worker
    prompts for cron rather than inheriting the whole-server grant."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(
        json.dumps({"name": "kirocrew", "allowedTools": ["@kirocrew-cron", "fs_read"]}),
        encoding="utf-8",
    )
    kept = agent._apply_worker_exclusions(["@kirocrew-cron", "fs_read"], template_grants=[])
    assert kept == ["fs_read"]


def test_every_other_mirrored_server_keeps_its_whole_server_grant(worker_from_installed_default):
    """The subtraction is cron scheduling and nothing else: a user's own servers are
    inherited with the same entries the default has for them."""
    allowed = worker_from_installed_default()["allowedTools"]
    for ref in ("@builder-mcp", "@pdf"):
        assert ref in allowed, ref


# ── governance surfaces the mirror must not widen ──────────────────────────


def test_a_mirrored_server_auto_approve_is_stripped(tmp_path, monkeypatch):
    """``autoApprove`` on an ``mcpServers`` entry is the SECOND way a call skips the
    PreToolUse gate, and no amount of ``allowedTools`` filtering touches it. The
    mirror copies the default's map verbatim, so the same governance pass
    ``rebuild_agent_config`` runs over the primary spec has to run here."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["builder-mcp"]["autoApprove"] = ["some_tool"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    seen: list[dict] = []

    def _recording_strip(servers):
        seen.append(servers)
        return {
            name: {k: v for k, v in e.items() if k != "autoApprove"} for name, e in servers.items()
        }

    monkeypatch.setattr(agent, "_strip_ungoverned_auto_approve", _recording_strip)
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    assert seen, "the governance pass never ran over the worker's server map"
    assert "builder-mcp" in seen[0], "the pass ran before the mirror, not after it"
    assert "autoApprove" not in worker["mcpServers"]["builder-mcp"]


def test_every_read_and_the_write_sit_in_one_critical_section(tmp_path, monkeypatch):
    """Both reads and the write are ONE unit. ``agents_spec_lock`` is what every other
    template-spec read-modify-writer in this module holds, so a dashboard edit cannot
    land between the worker read and the worker write. The DEFAULT read belongs inside
    the same section too: a mirror snapshot taken before it would already be stale by
    the write, which is how a deregistered server's grant stays auto-approved."""
    import contextlib

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    (tmp_path / WORKER_AGENT_FILENAME).write_text(
        json.dumps(_stale_worker_spec()), encoding="utf-8"
    )

    held: list[bool] = [False]
    order: list[str] = []

    @contextlib.contextmanager
    def _recording_lock(agents_dir):
        held[0] = True
        order.append("lock")
        try:
            yield
        finally:
            held[0] = False
            order.append("unlock")

    real_read = agent._read_spec_capped
    real_write = agent._atomic_json_write

    def _read(path):
        order.append(f"read:{path.name}:{held[0]}")
        return real_read(path)

    def _write(path, data):
        order.append(f"write:{held[0]}")
        return real_write(path, data)

    monkeypatch.setattr(agent, "agents_spec_lock", _recording_lock)
    monkeypatch.setattr(agent, "_read_spec_capped", _read)
    monkeypatch.setattr(agent, "_atomic_json_write", _write)
    agent._install_worker_agent()

    # Exactly ONE read of the default, and the mirrored-from stamp does not add a second:
    # it hashes the bytes this derivation already mirrored. A stamp that went back to the
    # file would be a separate observation, so it could record a fingerprint for a
    # generation the spec on disk does not mirror.
    assert order == [
        "lock",
        f"read:{AGENT_FILENAME}:{True}",
        f"read:{WORKER_AGENT_FILENAME}:{True}",
        f"write:{True}",
        "unlock",
    ], order
    assert order.count(f"read:{AGENT_FILENAME}:{True}") == 1, order


def test_the_withheld_cron_grants_are_sel_audited(tmp_path, monkeypatch):
    """Withholding a grant is a permission DECISION, and every other writer of an
    ``allowedTools`` list emits this event for it. Without it a worker silently starts
    prompting for a verb the default auto-approves, with no record of which rule did
    it."""
    records: list[dict] = []

    class _Recorder:
        def log_api_access(self, **kwargs):
            records.append(kwargs)

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "sel", lambda: _Recorder())
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    withheld = [
        r
        for r in records
        if r.get("operation") == "mcp_auto_approve_withheld"
        and "kirocrew-cron" in str(r.get("resources"))
    ]
    assert withheld, f"no withhold event names the cron narrowing: {records}"
    assert "narrowed to" in str(withheld[0]["resources"])


def test_an_audit_failure_never_breaks_the_install(tmp_path, monkeypatch):
    """Same footing as every other audit in this module: an unwritable SEL log must
    cost one record, not the agent a conductor dispatches to."""

    class _Broken:
        def log_api_access(self, **kwargs):
            raise OSError("audit sink unavailable")

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "sel", lambda: _Broken())
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    assert (tmp_path / WORKER_AGENT_FILENAME).is_file()


# ── an opt-in set is assigned per agent, not inherited ─────────────────────


def test_an_opt_in_set_mounted_on_the_default_does_not_reach_the_worker(
    worker_from_installed_default,
):
    """``kirocrew-dashboard`` carries ``session_send``, and a worker's inability to
    reach it is STRUCTURAL rather than withheld: the ledger is a worker's only channel
    to its conductor. Mirroring the server would make that guarantee conditional on
    what the operator happens to have mounted on their own agent."""
    worker = worker_from_installed_default()
    assert "kirocrew-dashboard" in _DEFAULT_SPEC_ON_DISK["mcpServers"]
    assert "kirocrew-dashboard" not in worker["mcpServers"]
    assert "@kirocrew-dashboard" not in worker["tools"]
    assert not [ref for ref in worker["allowedTools"] if "kirocrew-dashboard" in ref]
    assert "kirocrew-dashboard" not in json.dumps(worker["permissions"])


def test_the_work_server_is_exempt_from_that_exclusion(worker_from_installed_default):
    """``kirocrew-work`` is opt-in too, and this installer ASSIGNS it — which is the
    one way an opt-in set is meant to arrive. An exclusion that ate it would leave the
    worker unable to report at all."""
    worker = worker_from_installed_default()
    assert "kirocrew-work" not in agent._worker_unassignable_servers()
    assert worker["mcpServers"]["kirocrew-work"]["args"][-1] == "mcp-work"
    assert "@kirocrew-work" in worker["tools"]


def test_the_unassignable_set_is_derived_from_the_registry(monkeypatch):
    """Derived rather than listed, so an opt-in server added tomorrow is withheld by
    default instead of reaching the worker until somebody notices."""
    assert agent._worker_unassignable_servers() == frozenset({"kirocrew-dashboard"})
    monkeypatch.setitem(
        agent._MANAGED_MCP_SERVERS,
        "kirocrew-hypothetical",
        {"invocation_fn": lambda: ("kirocrew", ["mcp-hypothetical"]), "opt_in": True},
    )
    assert "kirocrew-hypothetical" in agent._worker_unassignable_servers()


def test_dropping_a_server_leaves_the_always_on_ones_alone(worker_from_installed_default):
    """The subtraction is opt-in sets and nothing else: an always-on managed server and
    a user's own server are inherited exactly as the default has them."""
    worker = worker_from_installed_default()
    assert "kirocrew-core" in worker["mcpServers"]
    assert "builder-mcp" in worker["mcpServers"]
    assert "@builder-mcp" in worker["allowedTools"]


def test_the_unmirrored_set_is_sel_audited(tmp_path, monkeypatch):
    """A set the default agent holds and the worker does not is a permission decision,
    on the same footing as every other one this installer makes."""
    records: list[dict] = []

    class _Recorder:
        def log_api_access(self, **kwargs):
            records.append(kwargs)

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "sel", lambda: _Recorder())
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    unmirrored = [r for r in records if "not mirrored onto the worker" in str(r.get("resources"))]
    assert unmirrored, f"no record names the unmirrored set: {records}"
    assert "kirocrew-dashboard" in str(unmirrored[0]["resources"])


# ── an unreadable sidecar must not clobber a pin ───────────────────────────


def test_an_unreadable_sidecar_keeps_the_worker_spec_model(
    worker_from_installed_default, tmp_path, monkeypatch
):
    """A sidecar that is PRESENT but will not parse means ownership is UNKNOWN, and the
    two answers are not symmetric: mirroring over a pin destroys a value recorded
    nowhere else (the sidecar holds the flag, the spec holds the model), while
    declining to mirror leaves a stale model the next readable refresh heals."""
    worker_from_installed_default()
    pinned = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    pinned["model"] = "claude-haiku-4.5"
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(pinned), encoding="utf-8")

    def _unreadable(name, strict=False):
        if strict:
            raise ValueError("sidecar does not hold a JSON object")
        return None

    monkeypatch.setattr(agent.agent_state, "get_model_managed", _unreadable)
    assert worker_from_installed_default()["model"] == "claude-haiku-4.5"


def test_a_missing_sidecar_entry_still_lets_the_mirror_heal_a_stale_model(
    worker_from_installed_default, tmp_path, monkeypatch
):
    """The other side of that guard, and the reason it cannot simply refuse to write:
    no entry at all is the legacy state EVERY worker spec written before this
    reads as, and those stale ``auto`` files are what the mirror exists to heal."""
    stale = _stale_worker_spec()
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(agent.agent_state, "get_model_managed", lambda name, strict=False: None)
    assert worker_from_installed_default()["model"] == "claude-opus-5"


def test_the_strict_read_reaches_the_sidecar_module(tmp_path, monkeypatch):
    """Pinned against the real getter rather than a stub, because the fix is the
    ``strict`` keyword existing there at all: ``agent_state._read`` already states this
    rule for its mutators, and this caller's answer feeds a write."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / agent_state._STATE_FILENAME).write_text("{not json", encoding="utf-8")
    assert agent_state.get_model_managed("kirocrew-worker") is None
    with pytest.raises(ValueError):
        agent_state.get_model_managed("kirocrew-worker", strict=True)


# ── the default spec's own writer lock, and the second gate-skip channel ───


def test_the_default_specs_own_writer_lock_is_held_too(tmp_path, monkeypatch):
    """``kirocrew.json`` has two independent-locked writers, and the app MCP
    registration path takes ``bridges._mcp_lock`` for its read-modify-write of that
    file rather than ``agents_spec_lock``. So holding only the agents lock leaves the
    mirror racing a deregistration: a server removed from the default after the
    snapshot keeps its auto-approved grant on the worker."""
    import contextlib

    from kiro_crew.apps import bridges

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")

    taken: list[str] = []

    @contextlib.contextmanager
    def _recording_mcp_lock(*args, **kwargs):
        taken.append("mcp")
        yield

    @contextlib.contextmanager
    def _recording_agents_lock(agents_dir):
        taken.append("agents")
        yield

    monkeypatch.setattr(bridges, "_mcp_lock", _recording_mcp_lock)
    monkeypatch.setattr(agent, "agents_spec_lock", _recording_agents_lock)
    agent._install_worker_agent()

    # Order is load-bearing, not incidental: nothing else in the tree nests these two,
    # so this installer establishes it — the file it WRITES outermost, the file it
    # READS innermost — and a future nester matches it or risks a deadlock.
    assert taken == ["agents", "mcp"], taken

    # The public re-derive seam takes the same two in the same order, which is why it
    # documents that a caller must not already hold the inner one.
    taken.clear()
    assert agent.rederive_worker_agent("a test") is True
    assert taken == ["agents", "mcp"], taken


def test_a_mirrored_auto_approve_cannot_carry_an_excluded_cron_verb(tmp_path, monkeypatch):
    """The subtraction's other channel. ``autoApprove`` skips the PreToolUse gate
    without ever touching ``allowedTools``, so filtering grants alone leaves
    ``cron_add`` auto-approved — and the ceiling pass does not close it either,
    because that one is whole-server and keeps the key whenever the server is
    allowed."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["kirocrew-cron"]["autoApprove"] = [
        "cron_list",
        "cron_add",
        "cron_update",
        "cron_secret_request",
    ]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    # Per VERB, not per key: the reading verb the default auto-approves survives, which
    # is the same line the grant exclusion draws.
    assert worker["mcpServers"]["kirocrew-cron"]["autoApprove"] == ["cron_list"]


def test_an_auto_approve_of_nothing_but_excluded_verbs_loses_the_key(tmp_path, monkeypatch):
    """Absent and empty mean the same thing to the runtime, so the shorter spec is the
    honest one."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["kirocrew-cron"]["autoApprove"] = ["cron_add"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "autoApprove" not in worker["mcpServers"]["kirocrew-cron"]


def test_an_unrelated_servers_auto_approve_is_left_alone(tmp_path, monkeypatch):
    """This pass is the worker's cron policy, not a second ceiling: it touches only the
    servers the exclusion names, and the governance pass keeps its own job."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["builder-mcp"]["autoApprove"] = ["cron_add", "some_tool"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(agent, "_strip_ungoverned_auto_approve", lambda servers: servers)
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    # ``cron_add`` here names no cron server, so the worker policy has nothing to say
    # about it — the ceiling is what governs this entry.
    assert worker["mcpServers"]["builder-mcp"]["autoApprove"] == ["cron_add", "some_tool"]


def test_the_withheld_auto_approve_verbs_are_sel_audited(tmp_path, monkeypatch):
    """Same footing as the grant exclusion: revoking a gate exemption is a permission
    decision an operator has to be able to find."""
    records: list[dict] = []

    class _Recorder:
        def log_api_access(self, **kwargs):
            records.append(kwargs)

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "sel", lambda: _Recorder())
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["kirocrew-cron"]["autoApprove"] = ["cron_add"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()

    hits = [r for r in records if "mirrored autoApprove" in str(r.get("resources"))]
    assert hits, f"no record names the withheld autoApprove: {records}"
    assert "kirocrew-cron/cron_add" in str(hits[0]["resources"])


# ── the mirror validates shape and mirrors restrictions, not just grants ───


def test_a_restriction_on_the_default_is_mirrored_with_the_grants(tmp_path, monkeypatch):
    """``excludedTools`` excludes a tool even when ``tools`` allows it, so mirroring
    the grant list alone inverts the parity claim: "superset of what the default
    GRANTS" must not become "superset of what the default PERMITS"."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = ["fs_read", "execute_bash"]
    spec["excludedTools"] = ["execute_bash"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert worker["excludedTools"] == ["execute_bash"]


def test_a_wildcard_cron_grant_is_narrowed_like_a_whole_server_one(tmp_path, monkeypatch):
    """``@kirocrew-cron/*`` and ``@kirocrew-cron`` grant the same thing, and only one of
    them looks like it. A subtraction matching one spelling leaves the other as a back
    door to everything it withheld."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = ["fs_read", "@kirocrew-cron/*"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    allowed = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))[
        "allowedTools"
    ]
    assert "@kirocrew-cron/*" not in allowed
    assert "@kirocrew-cron" not in allowed
    assert "@kirocrew-cron/cron_list" in allowed
    assert not [ref for ref in allowed if ref.endswith("/cron_add")]


def test_the_whole_server_notion_covers_every_spelling_and_nothing_else():
    """Three spellings mean the whole server, and it stays a named notion because a
    whole-server grant is the one case the subtraction can NARROW rather than drop."""
    for ref in ("@kirocrew-cron", "@kirocrew-cron/", "@kirocrew-cron/*"):
        assert agent._whole_server_ref(ref) == "kirocrew-cron", ref
    for ref in ("@kirocrew-cron/cron_list", "@kirocrew-cron/cron_*", "fs_read", "@", "@/cron_add"):
        assert agent._whole_server_ref(ref) is None, ref


def test_a_wildcard_auto_approve_on_an_excluded_server_loses_the_key(tmp_path, monkeypatch):
    """``autoApprove: ["*"]`` covers the excluded verbs and cannot be narrowed — the
    field holds NAMES, so "everything except cron_add" has no spelling in it. Dropping
    it is the same fail-closed direction the grant substitution takes with nothing to
    substitute: the tools stay mounted and reach the approval gate."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["kirocrew-cron"]["autoApprove"] = ["*"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "autoApprove" not in worker["mcpServers"]["kirocrew-cron"]


def test_a_malformed_mirrored_key_is_not_mirrored_at_all(tmp_path, monkeypatch):
    """A spec is a hand-editable JSON file, so a key can hold anything. Every pass
    downstream guards with ``isinstance`` and SKIPS what it does not recognise, which
    fails OPEN: a ``mcpServers`` holding a list would reach the worker with no server
    dropped, no ``autoApprove`` stripped and no KAS rule derived. Validating at the
    boundary means the template's own value stands instead."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"] = ["not", "a", "map"]
    spec["allowedTools"] = "not a list"
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    assert isinstance(worker["mcpServers"], dict)
    assert isinstance(worker["allowedTools"], list)
    # The template's own value stood, so the work server still landed and the spec is
    # usable rather than a copy of a spec kiro-cli would reject.
    assert worker["mcpServers"]["kirocrew-work"]["args"][-1] == "mcp-work"
    assert "@kirocrew-work/work_brief" in worker["allowedTools"]


def test_a_boolean_model_is_not_mirrored_as_a_string(tmp_path, monkeypatch):
    """``bool`` is a subclass of ``int`` and reads as a scalar in JSON, so a shape check
    written loosely lets ``"model": true`` through into a field the resolver reads as a
    model id."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["model"] = True
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert worker["model"] is not True
    assert isinstance(worker["model"], str)


# ── the subtraction is closed under spelling, not a list of spellings ──────


@pytest.mark.parametrize(
    "entry",
    [
        "@kirocrew-cron/cron_*",
        "@kirocrew-cron/*add*",
        "@kirocrew-cron/cron_?dd",
        "@kirocrew-cron/CRON_ADD",
        "@kirocrew-cron/cron_add",
    ],
)
def test_any_grant_pattern_that_reaches_an_excluded_verb_is_withheld(entry, tmp_path, monkeypatch):
    """The defect every earlier version shared: each matched a SPELLING -- the exact
    ref, then the bare server, then ``/*`` -- and a partial-verb glob is none of those
    while still granting ``cron_add``. The predicate asks what the entry would MATCH,
    which is closed under spelling."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = ["@builder-mcp", entry]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    assert entry not in worker["allowedTools"], entry
    # The control: a mirrored grant the exclusion cannot reach is untouched, so the
    # entry above is gone because it reaches a cron verb and not because the pass is
    # dropping whatever it does not recognise.
    assert "@builder-mcp" in worker["allowedTools"]
    # And nothing that reaches the verb came back under another name.
    assert not [
        ref
        for ref in worker["allowedTools"]
        if agent._pattern_reaches_excluded(agent._canonical_grant_pattern(ref) or "")
    ]


@pytest.mark.parametrize("entry", ["*", "*add*", "cron_*", "cron_add", "?ron_add", "**"])
def test_a_bare_glob_that_reaches_an_excluded_verb_is_dropped(entry, tmp_path, monkeypatch):
    """The hole a `@`-only predicate leaves. `allowedTools: ["*"]` is kiro-cli's spelling
    for "every tool", so it auto-approves `cron_add` -- and the canonicaliser answered
    `None` for it, which the caller read as "reaches nothing" and passed it through
    untouched. An entry with no `@` is a glob over the WHOLE namespace: it reaches
    FURTHER than any server-scoped ref, not less far."""
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = [entry, "@pdf"]
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    # Dropped, not narrowed: there is no spelling for "everything except cron_add".
    assert entry not in worker["allowedTools"], worker["allowedTools"]
    # Fail-CLOSED, never fail-open: the tool stays mounted and its calls reach the gate.
    assert "@kirocrew-cron" in worker["tools"]
    # An unrelated grant in the same list is untouched -- the subtraction is cron
    # scheduling, not a general narrowing of what the default granted.
    assert "@pdf" in worker["allowedTools"]


def test_a_bare_entry_that_cannot_reach_an_excluded_verb_survives():
    """The other half, so the fix is a CLASSIFICATION and not a blanket refusal of
    non-`@` entries. Asserted on the subtraction itself rather than end to end, because
    whether a builtin grant survives the governance CEILING is a property of the host's
    policy and would make this assertion say something about the machine instead of about
    the subtraction."""
    survivors = ["fs_read", "fs_*", "@kirocrew-cron/cron_list", "@pdf"]
    assert agent._apply_worker_exclusions(survivors, template_grants=[]) == survivors


def test_a_dropped_bare_glob_is_reported_with_the_refs_it_reached(tmp_path, monkeypatch):
    """A withheld grant is a permission DECISION, and this one withholds the widest entry
    a spec can carry -- so the record has to name which excluded refs it reached."""
    records: list[dict] = []

    class _Recorder:
        def log_api_access(self, **kwargs):
            records.append(kwargs)

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = ["*"]
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "sel", lambda: _Recorder())
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()

    withheld = [r for r in records if "not auto-approved on the worker" in str(r.get("resources"))]
    assert withheld, f"a dropped namespace-wide glob must be recorded: {records}"
    resources = str(withheld[0]["resources"])
    assert "@kirocrew-cron/cron_add" in resources
    assert "reaches" in resources


def test_the_grant_predicate_answers_for_every_entry_shape():
    """It classifies, it never abstains. The predicate is the whole subtraction, so an
    entry it declines to judge is a grant nobody looked at."""
    for entry in (
        "*",
        "**",
        "fs_read",
        "cron_add",
        "@kirocrew-cron",
        "@kirocrew-cron/cron_list",
        "@",
        "@/cron_add",
        "",
        "@kirocrew-cron/*",
    ):
        answer = agent._grant_reaches_excluded(entry)
        assert isinstance(answer, list), entry
    assert agent._grant_reaches_excluded("*") == sorted(agent._WORKER_EXCLUDED_GRANTS)
    assert agent._grant_reaches_excluded("*add*") == ["@kirocrew-cron/cron_add"]
    assert agent._grant_reaches_excluded("fs_read") == []
    assert agent._grant_reaches_excluded("@kirocrew-cron/cron_list") == []


def test_the_grant_predicate_has_no_early_exit_that_skips_classification():
    """The zero-shortcut discipline, by AST rather than by reading. Every earlier version of
    this subtraction grew an early exit for a shape it did not want to think about, and each
    of those was a grant reaching an excluded verb unexamined -- the `@`-only canonicaliser
    being the last one."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(agent._grant_reaches_excluded)))
    fn = tree.body[0]
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) == 1, f"exactly one exit, taken after classifying: {len(returns)}"
    assert returns[0] is fn.body[-1], "the only return must be the last statement"
    assert not [
        n for n in ast.walk(fn) if isinstance(n, (ast.Break, ast.Continue))
    ], "a break or continue here skips an excluded ref without judging it"


def test_a_glob_that_cannot_reach_an_excluded_verb_passes_through(tmp_path, monkeypatch):
    """The other half, and the one that keeps this a subtraction rather than a
    narrowing: an entry the exclusion cannot touch is left exactly as the default has
    it."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = ["@builder-mcp", "@kirocrew-cron/cron_l*"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "@kirocrew-cron/cron_l*" in worker["allowedTools"]


def test_the_predicate_answers_which_excluded_refs_a_pattern_reaches():
    """Asserted directly, because it is the one place the subtraction's completeness
    lives and every caller trusts it."""
    assert agent._pattern_reaches_excluded("@kirocrew-cron/*") == [
        "@kirocrew-cron/cron_add",
        "@kirocrew-cron/cron_secret_request",
        "@kirocrew-cron/cron_update",
    ]
    assert agent._pattern_reaches_excluded("@kirocrew-cron/cron_a*") == ["@kirocrew-cron/cron_add"]
    assert agent._pattern_reaches_excluded("@kirocrew-cron/cron_l*") == []
    assert agent._pattern_reaches_excluded("@kirocrew-core/*") == []
    # Case-folded too: matching MORE is the safe direction, since a match only ever
    # withholds a grant.
    assert agent._pattern_reaches_excluded("@KIROCREW-CRON/CRON_ADD") == ["@kirocrew-cron/cron_add"]


def test_the_canonical_form_collapses_the_whole_server_spellings():
    assert agent._canonical_grant_pattern("@kirocrew-cron") == "@kirocrew-cron/*"
    assert agent._canonical_grant_pattern("@kirocrew-cron/") == "@kirocrew-cron/*"
    assert agent._canonical_grant_pattern("@kirocrew-cron/*") == "@kirocrew-cron/*"
    assert agent._canonical_grant_pattern("@kirocrew-cron/cron_*") == "@kirocrew-cron/cron_*"
    assert agent._canonical_grant_pattern("fs_read") is None
    assert agent._canonical_grant_pattern("@") is None


@pytest.mark.parametrize("approved", [["cron_*"], ["*"], ["*add*"], ["cron_?dd"]])
def test_an_auto_approve_pattern_that_reaches_an_excluded_verb_is_dropped(
    approved, tmp_path, monkeypatch
):
    """The identical predicate on the other channel: an ``autoApprove`` name is a
    pattern over that server's verbs, so the same globs reach the same verbs."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["kirocrew-cron"]["autoApprove"] = list(approved)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "autoApprove" not in worker["mcpServers"]["kirocrew-cron"], approved


def test_an_auto_approve_naming_only_reading_verbs_survives(tmp_path, monkeypatch):
    """Nothing is narrowed for its own sake: a name that cannot reach an excluded verb
    keeps its exemption."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["kirocrew-cron"]["autoApprove"] = ["cron_list", "cron_pause"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert worker["mcpServers"]["kirocrew-cron"]["autoApprove"] == ["cron_list", "cron_pause"]


def test_a_withheld_pattern_says_which_verbs_it_reached(tmp_path, monkeypatch):
    """The audit has to name the reason, not just the entry: an operator reading it
    needs to know WHY a grant the default holds is not on the worker."""
    records: list[dict] = []

    class _Recorder:
        def log_api_access(self, **kwargs):
            records.append(kwargs)

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent, "sel", lambda: _Recorder())
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["allowedTools"] = ["@kirocrew-cron/cron_*"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()

    hits = [r for r in records if "cron_*" in str(r.get("resources"))]
    assert hits, f"no record names the withheld pattern: {records}"
    assert "reaches" in str(hits[0]["resources"])
    assert "cron_add" in str(hits[0]["resources"])


# ── a worker never STARTS on a mirror older than the default ───────────────


def test_a_failed_re_derive_never_fails_the_app_operation(tmp_path, monkeypatch):
    """An app operation must not fail because a derived spec could not be rewritten: a
    stale worker spec is worse than a current one and better than a broken enable."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)

    def _boom() -> None:
        raise OSError("agents dir is read-only")

    monkeypatch.setattr(agent, "_install_worker_agent", _boom)
    assert agent.rederive_worker_agent("a test") is False


def test_the_re_derive_seam_takes_only_a_reason(monkeypatch):
    """A caller that had to hand over a config or a path could hand over the WRONG one,
    and the whole point is that the derivation reads the installed default itself."""
    import inspect

    params = list(inspect.signature(agent.rederive_worker_agent).parameters)
    assert params == ["reason"]


def test_the_spawn_path_re_derives_a_stale_mirror_before_starting(tmp_path, monkeypatch):
    """The property that replaces per-writer hooks: `kirocrew.json` has six write
    sites across three modules under two different file locks, so a re-derive hung off
    each writer leaks one hole per writer nobody named. The spawn is ONE place, covers
    a writer added tomorrow, and cannot lose the race a post-write hook can."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"]["a-revoked-app:server"] = {"command": "revoked", "args": []}
    spec["allowedTools"].append("@a-revoked-app:server")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent._install_worker_agent()
    assert (
        "a-revoked-app:server"
        in json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))["mcpServers"]
    )

    # A writer with no hook scrubs the default — the dashboard agent-config PUT, the
    # MCP sync handler, the bridges reconcile pass: none of them re-derive.
    scrubbed = json.loads((tmp_path / AGENT_FILENAME).read_text(encoding="utf-8"))
    del scrubbed["mcpServers"]["a-revoked-app:server"]
    scrubbed["allowedTools"] = [t for t in scrubbed["allowedTools"] if t != "@a-revoked-app:server"]
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(scrubbed), encoding="utf-8")

    agent.require_fresh_derived_spec("kirocrew-worker", None)

    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "a-revoked-app:server" not in worker["mcpServers"]
    assert "@a-revoked-app:server" not in worker["allowedTools"]


def test_a_fresh_mirror_costs_no_re_derive(tmp_path, monkeypatch):
    """On the spawn hot path, so the common case must not rewrite a spec: a mirror that
    already matches is left alone."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    called: list[str] = []
    monkeypatch.setattr(
        agent, "rederive_worker_agent", lambda reason: called.append(reason) or True
    )
    agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert called == []


def test_an_equal_mtime_with_different_content_still_re_derives(tmp_path, monkeypatch):
    """The case a timestamp check gets wrong, and the reason the signal is a CONTENT
    fingerprint: two writes inside one filesystem tick, a restored backup, or a clock
    that steps backwards all leave a stale mirror wearing a plausible mtime."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    worker = tmp_path / WORKER_AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("builder-mcp")
    spec["allowedTools"] = [t for t in spec["allowedTools"] if t != "@builder-mcp"]
    default.write_text(json.dumps(spec), encoding="utf-8")
    # Same mtime on both, to the nanosecond the platform records — the tie an
    # ordering check reads as "the mirror is not older, so it is fine".
    stamp = worker.stat().st_mtime_ns
    os.utime(default, ns=(stamp, stamp))
    assert default.stat().st_mtime == worker.stat().st_mtime

    agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "builder-mcp" not in json.loads(worker.read_text(encoding="utf-8"))["mcpServers"]


def test_a_failed_re_derive_refuses_the_spawn(tmp_path, monkeypatch):
    """Fails CLOSED, unlike its best-effort neighbour: a refused dispatch is
    recoverable and reportable, a worker running a revoked server is not. The error
    names the spec so the operator knows which file to look at."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("pdf")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    monkeypatch.setattr(agent, "rederive_worker_agent", lambda reason: False)
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert WORKER_AGENT_FILENAME in str(excinfo.value)
    assert AGENT_FILENAME in str(excinfo.value)


def test_an_unwritable_sidecar_is_recoverable_by_re_deriving(tmp_path, monkeypatch):
    """The one unverifiable input that is RECOVERABLE, and the reason a successful
    re-derive counts as positive establishment on its own: the sidecar is how freshness
    is proven cheaply next time, not what makes the spec correct. A re-derive reads the
    installed default and writes the mirror inside one locked section, so on success the
    spec on disk was built from the default as it stood."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("pdf")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    # The sidecar records nothing and accepts nothing, so no fingerprint can be
    # compared before or after.
    monkeypatch.setattr(agent.agent_state, "get_mirrored_stat", lambda name: None)
    monkeypatch.setattr(agent.agent_state, "get_mirrored_from", lambda name: None)

    def _unwritable(name, value):
        raise OSError("sidecar is read-only")

    monkeypatch.setattr(agent.agent_state, "set_mirrored_from", _unwritable)
    monkeypatch.setattr(agent.agent_state, "set_mirrored_stat", _unwritable)

    agent.require_fresh_derived_spec("kirocrew-worker", None)
    healed = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "pdf" not in healed["mcpServers"]


def test_a_non_derived_agent_is_not_checked(tmp_path, monkeypatch):
    """Every other spawn pays one string compare: the gate is for the specs that
    MIRROR another spec, and nothing else has that property."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    called: list[str] = []
    monkeypatch.setattr(
        agent, "rederive_worker_agent", lambda reason: called.append(reason) or True
    )
    for name in ("kirocrew", "kirocrew-conductor", "some-app--agent", None, ""):
        agent.require_fresh_derived_spec(name, None)
    assert called == []


def test_the_fingerprint_covers_the_mirrored_keys_and_nothing_else(tmp_path, monkeypatch):
    """Scoped to what the mirror copies, so an edit to a key the worker does not
    inherit does not force a pointless re-derive on every spawn."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    before = agent.default_spec_fingerprint()

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["description"] = "an edit to a key the worker never mirrors"
    default.write_text(json.dumps(spec), encoding="utf-8")
    assert agent.default_spec_fingerprint() == before

    spec["allowedTools"] = list(spec["allowedTools"]) + ["@something-new"]
    default.write_text(json.dumps(spec), encoding="utf-8")
    assert agent.default_spec_fingerprint() != before

    # Key ORDER is not content: the same mirrored surface hashes identically whichever
    # writer produced the file.
    reordered = {k: spec[k] for k in reversed(list(spec))}
    default.write_text(json.dumps(reordered), encoding="utf-8")
    assert agent.default_spec_fingerprint() == agent.default_spec_fingerprint()


def test_a_missing_default_spec_refuses_the_spawn(tmp_path, monkeypatch):
    """With no default spec there is neither a way to VERIFY the mirror nor a way to
    rebuild it, so passing would be a pass on an unverifiable spec. A path that
    legitimately spawns before the default exists must materialize it first — the
    worker gate is not the place to make that legal."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent._install_worker_agent()
    assert agent.default_spec_fingerprint() is None
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "missing" in str(excinfo.value)
    assert AGENT_FILENAME in str(excinfo.value)


def test_every_spawn_path_goes_through_the_freshness_gate():
    """Enumeration, so a spawn path added tomorrow cannot silently skip it: every call
    site of the materialization self-heal is also a call site of this gate.

    Counted by AST reference, not by a call-shaped text pattern. Three of the five sites
    hand these functions to ``asyncio.to_thread`` as a REFERENCE, so a call-shaped pattern
    scored them zero and silently exempted the very paths this test exists to enumerate,
    both subprocess spawners among them. An AST walk also ignores the identifier where it
    appears only in a docstring, which a text scan counts as a call site that is not one."""
    import re as _re

    def _refs(tree: ast.AST, name: str) -> int:
        return sum(
            1
            for n in ast.walk(tree)
            if (isinstance(n, ast.Name) and n.id == name)
            or (isinstance(n, ast.Attribute) and n.attr == name)
        )

    materialize = {}
    fresh = {}
    # Text prefilter, then AST for the candidates only. A module whose text never mentions
    # either name cannot reference it, so a file the corpus does not yield scores 0 for
    # both -- what the old `tree = None` branch recorded -- and the AST is left doing the
    # one job it is needed for: telling a real reference apart from a docstring mention.
    # `parsed_candidates` yields one tree at a time and retains none, so the ASTs do not
    # outlive the loop the way a per-path AST cache here did.
    for path, _source, tree in parsed_candidates(
        require_any=("ensure_agent_materialized", "require_fresh_derived_spec")
    ):
        if path.name == "agent.py":  # the definitions themselves, not a spawn path
            continue
        materialize[path] = _refs(tree, "ensure_agent_materialized")
        fresh[path] = _refs(tree, "require_fresh_derived_spec")

    callers = {p for p, n in materialize.items() if n}
    assert callers, "no spawn path calls the materialization self-heal at all"
    # ONE exemption, and it is not an opinion: the kiro harness materializes but does
    # not gate, because its spawn is gated by the single owner that calls it. The two
    # tests below check that claim -- the seam has exactly one caller in src, and that
    # caller gates exactly once -- so this entry cannot become a way to skip the gate.
    gated_by_the_owner = {"kiro.py"}
    missing = sorted(
        p.name for p in callers if not fresh.get(p) and p.name not in gated_by_the_owner
    )
    assert not missing, f"spawn path(s) skip the freshness gate: {missing}"
    exempt_but_gating = sorted(
        p.name for p in callers if fresh.get(p) and p.name in gated_by_the_owner
    )
    assert not exempt_but_gating, (
        f"{exempt_but_gating} gates AND is listed as gated by the owner -- that is the "
        f"double-gate this exemption exists to prevent"
    )

    # And each one passes its own work dir. A site that passed None would disable the
    # project-shadow refusal for that path while looking like it had the gate.
    blank = sorted(
        p.name
        for p in callers
        if fresh.get(p)
        and _re.search(r"require_fresh_derived_spec[,(][^)]*None", p.read_text(encoding="utf-8"))
    )
    assert not blank, f"spawn path(s) pass no work dir to the gate: {blank}"


def test_the_per_writer_hook_is_gone():
    """One runtime mechanism, not two. The app-registration hook is removed in favour
    of the spawn gate — keeping both would leave two answers to one question, and the
    hook is the one that cannot cover a writer it does not own."""
    from kiro_crew.apps import bridges

    assert not hasattr(bridges, "_rederive_derived_specs")


def test_the_mirrored_from_fingerprint_is_recorded_in_the_sidecar(tmp_path, monkeypatch):
    """In the sidecar rather than the spec: kiro-cli validates with
    ``deny_unknown_fields`` and DROPS a spec carrying a key it does not know, so
    bookkeeping written into the spec would cost the agent its existence."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    assert agent_state.get_mirrored_from("kirocrew-worker") == agent.default_spec_fingerprint()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "mirrored_from" not in worker


# ── the refusal has to ABORT, not be logged by a neighbouring handler ──────


def test_the_gate_is_not_inside_a_try_at_any_spawn_site():
    """The defect this pins: the gate was dropped INSIDE pre-existing
    ``except Exception: logger.warning`` blocks, so the fail-closed refusal was logged
    and the spawn proceeded on the stale spec. An AST check, not a grep, so the next
    best-effort wrapper cannot quietly re-swallow it: a call may sit inside a ``try``
    only when every handler of that ``try`` is narrow enough to name the exception.

    Both halves of the bracket are checked, because the post-load refusal is the one
    with the worse failure mode: swallowed there, the session keeps running on a spec
    nobody verified rather than merely starting on one."""
    gates = ("require_fresh_derived_spec", "require_unchanged_derived_spec")
    offenders: list[str] = []
    checked: set[str] = set()
    # Same narrowed read as the enumeration above: only a file whose text spells one of
    # the gates can hold a `try` around a call to it, and the tree is dropped after each
    # file instead of being retained per path.
    for path, _source, tree in parsed_candidates(require_any=gates):
        if path.name == "agent.py":  # the definitions themselves, not a spawn site
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            names = {
                n.id for child in node.body for n in ast.walk(child) if isinstance(n, ast.Name)
            } | {
                n.attr
                for child in node.body
                for n in ast.walk(child)
                if isinstance(n, ast.Attribute)
            }
            guarded = names & set(gates)
            if not guarded:
                continue
            checked |= guarded
            for handler in node.handlers:
                caught = handler.type
                label = getattr(caught, "id", None) or getattr(caught, "attr", None)
                if caught is not None and label not in ("Exception", "BaseException"):
                    continue
                # A broad handler is only a hazard when it SWALLOWS. One that re-raises
                # is a cleanup guard, which is exactly the shape the spawn path needs:
                # the runtime's `except BaseException` kills the process, reaps its PID
                # entries and its protected-PID shield, and then re-raises.
                if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
                    continue
                offenders.append(f"{path.name}:{node.lineno} swallows {label or 'everything'}")
    assert not offenders, f"the freshness refusal can be swallowed at: {offenders}"
    # The walk above only judges calls that sit inside a `try`; assert it actually saw
    # both gates, so a rename or a moved call cannot turn this into a vacuous pass.
    assert checked == set(gates), f"gate(s) never reached by the AST walk: {set(gates) - checked}"


def test_the_kas_session_projection_converts_the_refusal_into_its_abort_type():
    """The KAS session projection keeps a gate of its own: it is not a spawn, it is the
    ``session/new`` extras, and its answer becomes the session's whole tool surface. A
    refusal there must reach the caller as the type that ABORTS the session, not as a
    warning line beside the best-effort materialization."""
    import inspect

    from kiro_crew.acp.harness import kas

    src = inspect.getsource(kas)
    assert "except agent_mod.DerivedSpecStale as exc:" in src
    after = src.split("except agent_mod.DerivedSpecStale as exc:", 1)[1]
    assert "raise AcpRuntimeError(str(exc)) from exc" in after.split("\n\n", 1)[0]


def test_the_runtime_converts_the_spawn_refusal_into_its_abort_type():
    """The spawn gate moved to the runtime, so the conversion moved with it. Left raw,
    the refusal would reach callers as a bare ``RuntimeError`` where every other
    pre-spawn refusal on this path is an ``AcpRuntimeError``."""
    import inspect

    from kiro_crew.acp import runtime

    src = inspect.getsource(runtime.AcpRuntime._resolve_spawn_plan)
    assert "except DerivedSpecStale as exc:" in src
    after = src.split("except DerivedSpecStale as exc:", 1)[1]
    assert "raise AcpRuntimeError(str(exc)) from exc" in after.split("\n\n", 1)[0]


def test_the_client_converts_the_refusal_into_its_abort_type():
    """The same property on the third path, whose abort type is ``AcpError`` -- and on
    BOTH of its gates. The pre-spawn refusal and the post-load one are separate call
    sites with separate handlers, and either one left raising ``DerivedSpecStale`` raw
    would slip past every handler in ``ensure_ready``: the pre-spawn one aborting
    without the cleanup path, the post-load one leaving a live child behind."""
    import inspect

    from kiro_crew.acp import client

    src = inspect.getsource(client)
    handlers = src.split("except DerivedSpecStale as exc:")[1:]
    assert len(handlers) == 3, (
        f"expected the pre-spawn gate, the post-initialize check and the post-session "
        f"array check, got {len(handlers)}"
    )
    for after in handlers:
        assert "raise AcpError(str(exc)) from exc" in after.split("\n\n", 1)[0]


def test_the_spawn_gate_is_called_exactly_once_per_spawn():
    """Two calls is a DEFECT, not redundancy. The gate may re-derive, so a second call
    between the capture and the exec leaves the subprocess loading the NEWER spec while
    the post-handshake check compares against the OLDER snapshot -- and kills a session
    that was never stale. Counted by AST over the runtime's plan resolver and both
    harnesses' spawn seams, which together are one spawn."""
    import ast
    import inspect
    from pathlib import Path as _Path

    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.harness import base, kas, kiro

    def _gate_calls(module, class_name: str, method: str) -> int:
        source = _Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != class_name:
                continue
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == method:
                    return sum(
                        1
                        for n in ast.walk(child)
                        if isinstance(n, ast.Name) and n.id == "require_fresh_derived_spec"
                    ) + sum(
                        1
                        for n in ast.walk(child)
                        if isinstance(n, ast.Attribute) and n.attr == "require_fresh_derived_spec"
                    )
        raise AssertionError(f"{class_name}.{method} not found in {module.__name__}")

    assert (
        _gate_calls(runtime_mod, "AcpRuntime", "_resolve_spawn_plan") == 1
    ), "the runtime is the single owner of the spawn gate, so it calls it exactly once"
    for module, cls in ((kas, "KasHarness"), (kiro, "KiroHarness"), (base, "AcpHarness")):
        try:
            count = _gate_calls(module, cls, "resolve_spawn")
        except AssertionError:
            continue
        assert count == 0, (
            f"{cls}.resolve_spawn gates as well as the runtime -- a re-derive between "
            f"the two calls makes the post-handshake check kill a valid session"
        )

    # And it is the LAST verification before the process: nothing between it and the
    # exec can re-derive, and the host's materialization self-heal runs first, so a
    # missing default spec is repaired rather than refused.
    plan_src = inspect.getsource(runtime_mod.AcpRuntime._resolve_spawn_plan)
    assert plan_src.index("resolve_spawn(") < plan_src.index(
        "require_fresh_derived_spec"
    ), "the capture must come AFTER the harness's own pre-spawn work"


def test_the_harness_spawn_seam_is_reached_only_through_the_gated_owner():
    """What licenses the kiro harness not gating: its spawn seam has exactly one caller in
    the product, and that caller gates. A second caller would be an ungated spawn path,
    which is the hole the enumeration test's exemption would otherwise hide."""
    invocations: list[str] = []
    for path, source in _package_sources():
        for lineno, line in enumerate(source.splitlines(), 1):
            if "resolve_spawn(" not in line or "def resolve_spawn(" in line:
                continue
            invocations.append(f"{path.name}:{lineno}")
    assert (
        len(invocations) == 1
    ), f"the spawn seam is invoked from more than one place: {invocations}"
    assert invocations[0].startswith("runtime.py:"), invocations


def test_a_re_derive_during_the_hosts_own_pre_spawn_work_does_not_kill_the_session(
    tmp_path, monkeypatch
):
    """The defect the single owner closes, end to end. The default spec is legitimately
    edited while the host resolves its plan -- exactly what a second gate call there used
    to do by re-deriving. With one gate, taken after that work, the snapshot describes the
    spec the child will actually load, so the post-handshake check passes."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.harness.base import SpawnPlan

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"]["builder-mcp"] = {"command": "builder", "args": ["--v2"]}

    class _EditingHarness:
        async def resolve_spawn(self, ctx):
            # Any legitimate host-side edit that lands while the plan is being resolved:
            # it rewrites the default spec and re-derives the mirror from it.
            (tmp_path / AGENT_FILENAME).write_text(json.dumps(edited), encoding="utf-8")
            agent.rederive_worker_agent("test: host pre-spawn work")
            return SpawnPlan(argv=["/bin/true"])

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._agent = "kirocrew-worker"
    rt._work_dir = tmp_path / "wd"
    rt._model = None
    rt._sandbox_mode = "auto"
    rt._private_memory = False
    # ``_harness`` is a cached property over the backend, so the stub is installed
    # through the cache slot the runtime itself fills.
    rt._harness_resolved = _EditingHarness()
    rt._derived_spec_snapshot = None

    asyncio.run(rt._resolve_spawn_plan())
    # The snapshot describes the spec as it stands AFTER the host's work, so the session
    # survives. Under a capture taken before that work this raises and kills it.
    agent.require_unchanged_derived_spec(rt._derived_spec_snapshot)

    # And an edit landing after the single check still ends the session: the property is
    # "nothing changed since the last verification", not "nothing changed at all".
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale):
        agent.require_unchanged_derived_spec(rt._derived_spec_snapshot)


def test_the_kas_projection_is_built_from_the_spec_read_under_the_gate(tmp_path, monkeypatch):
    """The KAS twin of the in-process property. The harness reads the spec once, under the
    gate, and the builder performs no read of its own: a second read is a window no lock
    can close, since both halves are this process's own reads, and whatever lands in
    between reaches the session as its whole tool surface."""
    import inspect

    from kiro_crew.acp import kas_agents
    from kiro_crew.acp.harness import kas

    builder = inspect.getsource(kas_agents.build_kas_custom_agents)
    assert "load_agent_spec(" not in builder, (
        "the projection must not read the spec itself -- it is handed the one the caller "
        "verified under the gate"
    )

    extras = inspect.getsource(kas.KasHarness.session_extras)
    assert "require_fresh_derived_spec" in extras
    after_gate = extras.split("require_fresh_derived_spec", 1)[1]
    assert (
        "snapshot.spec" in after_gate
    ), "a derived agent must be projected from the gate's own verified spec"


def test_the_kas_projection_reads_nothing_after_the_gate(tmp_path, monkeypatch):
    """The KAS twin, asserted behaviourally: for a derived agent the harness performs zero
    reads of the spec and projects the gate's own object."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp import kas_agents as kas_agents_mod
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.types import ACP_BACKEND_KAS

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    import kiro_crew.config.paths as paths_mod

    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "require_fork_governance", lambda a, w: None)

    reads: list[str] = []
    monkeypatch.setattr(
        kas_agents_mod,
        "load_agent_spec",
        lambda _dir, _agent: reads.append(_agent) or {"name": _agent},
    )

    projected: list[dict] = []
    real_build = kas_agents_mod.build_kas_custom_agents

    def _capture(agents_dir, agent_id, spec, **kwargs):
        projected.append(spec)
        return real_build(agents_dir, agent_id, spec, **kwargs)

    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _capture)

    extras = asyncio.run(
        harness_for(ACP_BACKEND_KAS).session_extras(
            "kirocrew-worker", work_dir=str(tmp_path / "wd")
        )
    )

    assert extras.custom_agents
    assert reads == [], f"the harness re-read the spec after the gate: {reads}"
    snap = agent.require_fresh_derived_spec("kirocrew-worker", str(tmp_path / "wd"))
    assert snap is not None and projected == [snap.spec]


def test_the_kas_projection_cannot_be_called_without_a_verified_spec():
    """Positional and non-defaulted, for the same reason ``work_dir`` is on the gate: a
    defaulted parameter that fell back to reading the file would restore the window for
    any caller that forgot to pass one."""
    import inspect

    from kiro_crew.acp.kas_agents import build_kas_custom_agents

    params = inspect.signature(build_kas_custom_agents).parameters
    assert list(params)[:3] == ["agents_dir", "agent_id", "spec"]
    assert params["spec"].default is inspect.Parameter.empty
    assert params["spec"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


# ── the fast path is an identity test, never an ordering one ────────────────


def test_a_restored_older_backup_still_re_derives(tmp_path, monkeypatch):
    """The hole an ordering check leaves open, and the reason the fast path compares one
    file to ITSELF: a default spec rolled back to an older copy is "older" than the
    mirror while holding different content, so "the mirror is newer, therefore fresh"
    is false exactly when it matters."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    worker = tmp_path / WORKER_AGENT_FILENAME
    backup = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    backup["mcpServers"]["a-revoked-app:server"] = {"command": "revoked", "args": []}
    backup["allowedTools"].append("@a-revoked-app:server")

    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    # The backup is restored over the default and back-dated well before the mirror.
    default.write_text(json.dumps(backup), encoding="utf-8")
    old = worker.stat().st_mtime_ns - 10_000_000_000
    os.utime(default, ns=(old, old))
    assert default.stat().st_mtime < worker.stat().st_mtime

    agent.require_fresh_derived_spec("kirocrew-worker", None)

    healed = json.loads(worker.read_text(encoding="utf-8"))
    assert "a-revoked-app:server" in healed["mcpServers"], (
        "an older-but-different default must still be mirrored, or a rollback "
        "silently keeps the worker on content the default does not have"
    )


def test_an_unchanged_default_skips_the_hash_entirely(tmp_path, monkeypatch):
    """The fast path's whole job, on the spawn hot path: an untouched file is not read
    and not hashed."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    hashed: list[int] = []
    real = agent.default_spec_fingerprint
    monkeypatch.setattr(agent, "default_spec_fingerprint", lambda: hashed.append(1) or real())
    agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert hashed == [], "an unchanged default spec was hashed anyway"


def test_the_same_mtime_with_a_different_size_re_derives(tmp_path, monkeypatch):
    """Size rides along in the identity because a same-nanosecond rewrite is the
    ordinary case on a coarse clock, not an exotic one."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    worker = tmp_path / WORKER_AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    before = default.stat()

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("pdf")
    default.write_text(json.dumps(spec), encoding="utf-8")
    os.utime(default, ns=(before.st_mtime_ns, before.st_mtime_ns))
    assert default.stat().st_mtime_ns == before.st_mtime_ns
    assert default.stat().st_size != before.st_size

    agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "pdf" not in json.loads(worker.read_text(encoding="utf-8"))["mcpServers"]


def test_the_identity_is_one_files_own_stat_not_a_comparison(tmp_path, monkeypatch):
    """Asserted on the helper directly, because the whole defect was a comparison
    between two files: this reports one file's identity and takes no second file."""
    import inspect

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    assert agent.default_spec_identity() is None  # absent file, no fast path
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    st = (tmp_path / AGENT_FILENAME).stat()
    assert agent.default_spec_identity() == f"{st.st_mtime_ns}-{st.st_size}-{st.st_ino}"
    assert list(inspect.signature(agent.default_spec_identity).parameters) == []
    # No mtime-versus-mtime comparison survives in the gate.
    gate = inspect.getsource(agent.require_fresh_derived_spec)
    assert "st_mtime" not in gate


# ── the gate passes only on POSITIVELY established freshness ────────────────


def test_an_unreadable_default_spec_refuses_the_spawn(tmp_path, monkeypatch):
    """The fail-open branch this closes: `default_spec_fingerprint()` answers ``None``
    for "present but unreadable" as well as for "absent", and treating that as a pass
    let a spawn proceed on a mirror that could not be checked at all. Answering False
    instead would be no better — a re-derive that cannot read the default silently
    produces a TEMPLATE-based worker, freshly built and carrying none of the user's
    servers or revocations."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    (tmp_path / AGENT_FILENAME).write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "cannot be read" in str(excinfo.value)
    assert AGENT_FILENAME in str(excinfo.value)


def test_a_default_spec_refused_at_the_read_gate_refuses_the_spawn(tmp_path, monkeypatch):
    """Same branch, reached the other way: the capped reader refuses an oversized spec
    or one whose bytes are not UTF-8, and returns ``None`` exactly as a parse failure
    does. Both are "unverifiable", and neither is a pass."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    # The file changes, so the identity fast path cannot prove freshness, and the
    # reader then declines it the way the size cap does: nothing to compare.
    (tmp_path / AGENT_FILENAME).write_text(
        json.dumps(_DEFAULT_SPEC_ON_DISK) + "   ", encoding="utf-8"
    )
    monkeypatch.setattr(agent, "_read_spec_capped", lambda path: None)
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "cannot be read" in str(excinfo.value)


def test_the_gate_has_no_early_return_at_all(tmp_path):
    """By construction rather than by audit. Every earlier version of this check grew an
    early ``return`` for a case it could not evaluate — a missing default, an unreadable
    one — and each was a fail-OPEN pass on the one path where the mirror is
    unverifiable. The freshness body now has NO return statement, so falling off the end
    is reachable only after a verified match or a successful re-derive."""
    import ast
    import inspect
    import textwrap

    body = textwrap.dedent(inspect.getsource(agent._require_fresh_worker_spec))
    returns = [n for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Return)]
    assert not returns, f"the freshness body has {len(returns)} return(s); it must have none"


def test_the_entry_point_returns_either_not_applicable_or_a_verified_snapshot():
    """Exactly two exits, and they mean different things: ``None`` is the scope guard
    (nothing but the worker mirrors another spec, so there is no generation to be stale
    against) and a snapshot is a positive verdict. Kept distinguishable so a caller
    cannot read "not applicable" as "verified fresh" — the post-load check short-circuits
    on ``None`` precisely because the bracket does not apply there."""
    import ast
    import inspect
    import textwrap

    body = textwrap.dedent(inspect.getsource(agent.require_fresh_derived_spec))
    returns = [n for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Return)]
    assert len(returns) == 2

    # By SHAPE, not by walk order, which is not source order: one ``None`` for the scope
    # guard and one constructed snapshot for the positive verdict. ``return None`` parses
    # as a ``Constant``, a bare ``return`` as no value; both count as the guard.
    def _is_none(node: ast.Return) -> bool:
        return node.value is None or (
            isinstance(node.value, ast.Constant) and node.value.value is None
        )

    assert sum(1 for n in returns if _is_none(n)) == 1
    assert sum(1 for n in returns if isinstance(n.value, ast.Call)) == 1


def test_the_matcher_reports_only_a_proven_match(tmp_path, monkeypatch):
    """Asserted on the predicate directly: True means proven, False means "not proven,
    go rebuild", and unverifiable is neither."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    assert agent._derived_spec_matches_default("kirocrew-worker") is True

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("pdf")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    assert agent._derived_spec_matches_default("kirocrew-worker") is False

    (tmp_path / AGENT_FILENAME).write_text("not json at all", encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale):
        agent._derived_spec_matches_default("kirocrew-worker")


# ── a checkout may not supply the worker's spec ─────────────────────────────


def test_a_project_local_shadow_of_the_worker_refuses_the_spawn(tmp_path, monkeypatch):
    """kiro-cli resolves `--agent` against `<cwd>/.kiro/agents/*.json` as well as the
    user level, so a fresh, verified derivation in `~/.kiro/agents` proves nothing about
    the spec the session actually gets. A checkout shipping its own worker spec can
    declare any `autoApprove` it likes and no derivation here would touch it."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / AGENT_FILENAME).write_text(
        json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8"
    )
    agent._install_worker_agent()

    work = tmp_path / "checkout"
    (work / ".kiro" / "agents").mkdir(parents=True)
    shadow = work / ".kiro" / "agents" / WORKER_AGENT_FILENAME
    shadow.write_text(
        json.dumps(
            {
                "name": "kirocrew-worker",
                "mcpServers": {"evil": {"command": "x", "args": [], "autoApprove": ["*"]}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", str(work))
    assert str(shadow) in str(excinfo.value)


def test_a_shadow_is_matched_by_its_declared_name_not_its_filename(tmp_path, monkeypatch):
    """The declared `name` beats the filename in kiro-cli's own listing order, so a file
    called anything at all can shadow the worker."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / AGENT_FILENAME).write_text(
        json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8"
    )
    agent._install_worker_agent()

    work = tmp_path / "checkout"
    (work / ".kiro" / "agents").mkdir(parents=True)
    shadow = work / ".kiro" / "agents" / "totally-unrelated-name.json"
    shadow.write_text(json.dumps({"name": "kirocrew-worker"}), encoding="utf-8")

    with pytest.raises(agent.DerivedSpecStale):
        agent.require_fresh_derived_spec("kirocrew-worker", str(work))


def test_a_checkout_with_no_worker_spec_is_unaffected(tmp_path, monkeypatch):
    """The refusal is a shadow of THIS agent and nothing else: an ordinary checkout, and
    one carrying project agents of its own, both spawn normally."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / AGENT_FILENAME).write_text(
        json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8"
    )
    agent._install_worker_agent()

    work = tmp_path / "checkout"
    (work / ".kiro" / "agents").mkdir(parents=True)
    (work / ".kiro" / "agents" / "some-project-agent.json").write_text(
        json.dumps({"name": "some-project-agent"}), encoding="utf-8"
    )
    agent.require_fresh_derived_spec("kirocrew-worker", str(work))
    agent.require_fresh_derived_spec("kirocrew-worker", None)


def test_a_spec_one_directory_up_is_not_a_shadow(tmp_path, monkeypatch):
    """No parent walk, because there is nothing to match: kiro-cli resolves only the work
    directory's own `.kiro/agents`, so a spec a level up is not dispatchable and refusing
    it would refuse a spawn kiro-cli would run correctly."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / AGENT_FILENAME).write_text(
        json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8"
    )
    agent._install_worker_agent()

    parent = tmp_path / "repo"
    (parent / ".kiro" / "agents").mkdir(parents=True)
    (parent / ".kiro" / "agents" / WORKER_AGENT_FILENAME).write_text(
        json.dumps({"name": "kirocrew-worker"}), encoding="utf-8"
    )
    nested = parent / "packages" / "thing"
    nested.mkdir(parents=True)
    agent.require_fresh_derived_spec("kirocrew-worker", str(nested))


def test_the_shadow_check_runs_before_the_freshness_checks(tmp_path, monkeypatch):
    """Ordering matters: everything else reasons about the global pair, so a verified
    global derivation must not answer for a spec kiro-cli would resolve instead. With no
    default spec at all the shadow is still what gets reported."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path / "agents")
    (tmp_path / "agents").mkdir()

    work = tmp_path / "checkout"
    (work / ".kiro" / "agents").mkdir(parents=True)
    (work / ".kiro" / "agents" / WORKER_AGENT_FILENAME).write_text(
        json.dumps({"name": "kirocrew-worker"}), encoding="utf-8"
    )
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", str(work))
    assert "project checkout declares its own" in str(excinfo.value)


def test_the_gate_signature_requires_a_work_dir():
    """Positional, not defaulted: a site that could omit it would silently disable the
    shadow refusal for that path."""
    import inspect

    params = inspect.signature(agent.require_fresh_derived_spec).parameters
    assert list(params) == ["agent", "work_dir"]
    assert params["work_dir"].default is inspect.Parameter.empty


# ── the bracket around a load this process cannot lock ──────────────────────


def test_the_gate_returns_what_it_verified(tmp_path, monkeypatch):
    """The capture half. A caller that has to prove freshness STILL holds after another
    process has read the spec needs to know what was checked, and `None` (nothing mirrors
    anything) has to stay distinguishable from a verdict."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert snap is not None
    assert snap.identity == agent.default_spec_identity()
    assert snap.fingerprint == agent.default_spec_fingerprint()
    assert agent.require_fresh_derived_spec("kirocrew", None) is None


def test_a_default_changed_during_the_load_ends_the_session(tmp_path, monkeypatch):
    """The window the pair closes: the pre-check passes, then a revocation lands, and
    kiro-cli — another process, some milliseconds later — reads the pre-revocation spec.
    Holding a writer lock across a subprocess's read is not available, so the window is
    closed by DETECTION instead."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("builder-mcp")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_unchanged_derived_spec(snap)
    assert "changed during worker load" in str(excinfo.value)
    # Fingerprints are reported as short hashes, not as spec contents.
    assert snap.fingerprint[:12] in str(excinfo.value)
    assert "builder-mcp" not in str(excinfo.value)


def test_an_unchanged_default_passes_the_post_load_check(tmp_path, monkeypatch):
    """The ordinary case, which must cost nothing and say nothing."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)
    agent.require_unchanged_derived_spec(snap)


def test_a_post_load_check_that_cannot_read_ends_the_session(tmp_path, monkeypatch):
    """Fail closed on the re-check itself: an unreadable default here is not "probably
    fine", it is the one state in which this function cannot do its job, and the session
    it guards is already running on a spec it cannot vouch for."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    (tmp_path / AGENT_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_unchanged_derived_spec(snap)
    assert "became unreadable" in str(excinfo.value)


def test_the_post_load_check_is_not_applicable_without_a_snapshot():
    """`None` is "the bracket does not apply", not "satisfied" — the pre-check answers it
    for every agent that mirrors nothing, and the post-check must not invent a verdict."""
    agent.require_unchanged_derived_spec(None)


def test_the_runtime_closes_the_bracket_after_the_handshake():
    """Placement is the whole mechanism: the check has to sit after the point kiro-cli has
    provably READ its spec — the `initialize` response — and inside the guard that kills
    the process, since a session that may have loaded an unverified spec must not
    survive."""
    import inspect

    from kiro_crew.acp import runtime

    src = inspect.getsource(runtime.AcpRuntime)
    assert "require_unchanged_derived_spec" in src
    before, after = src.split("require_unchanged_derived_spec", 1)
    # The handshake precedes it.
    assert '"initialize"' in before
    # And the kill-and-re-raise guard encloses it.
    assert "except BaseException:" in after
    assert "failed init handshake cleanup" in after


def test_the_snapshot_is_a_coherent_pair_from_one_observation(tmp_path, monkeypatch):
    """The defect a two-observation snapshot has: an identity from a fresh stat beside a
    fingerprint read out of the sidecar is a TORN pair -- the NEW identity carrying the
    OLD content's hash -- and the post-load check then accepts the new default while the
    subprocess loaded the old spec. Here the default moves during the verification, and the
    pair that comes back describes ONE generation of the file, whichever one it settled on."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")

    real_read = agent._installed_default_spec
    moved: list[bool] = []

    def _read_then_move():
        spec = real_read()
        if not moved:
            # The write lands between the identity stat and the fingerprint's own read --
            # exactly the instant a two-observation pair tears at.
            moved.append(True)
            default.write_text(json.dumps(edited), encoding="utf-8")
        return spec

    monkeypatch.setattr(agent, "_installed_default_spec", _read_then_move)
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert moved, "the reader never fired, so this test proved nothing"
    assert snap is not None

    # COHERENT: the identity and the fingerprint describe the same generation. Asserted
    # against the file as it now stands rather than against either expected value, so the
    # test states the property instead of the outcome.
    monkeypatch.setattr(agent, "_installed_default_spec", real_read)
    assert (snap.identity, snap.fingerprint) == (
        agent.default_spec_identity(),
        agent.default_spec_fingerprint(),
    ), "the snapshot pairs an identity with a fingerprint from a different generation"

    # And the bracket still does its job: a change after the observation ends the session.
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    with pytest.raises(agent.DerivedSpecStale):
        agent.require_unchanged_derived_spec(snap)


def test_the_snapshot_never_takes_its_fingerprint_from_the_sidecar():
    """The sidecar is bookkeeping for the fast path that avoids a RE-DERIVE, not evidence
    about the file as it stands now. Read into the snapshot it is a second observation, so
    the gate hashes the bytes it read instead -- one hash of a small file, on a path that
    is already spawning a process."""
    import inspect

    src = inspect.getsource(agent.require_fresh_derived_spec)
    assert (
        "get_mirrored_from" not in src
    ), "the snapshot's fingerprint must be of the bytes the gate read, not a recorded value"
    assert "_spec_fingerprint(" in src


def test_a_default_spec_that_never_settles_refuses_the_spawn(tmp_path, monkeypatch):
    """Bounded, and fail-closed at the bound. The loop's exit condition is another process
    leaving the file alone, so unbounded it would spin on a host rewriting the spec in a
    loop -- and a spawn that never returns is worse than one that refuses."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    stats: list[int] = []

    def _never_the_same():
        stats.append(len(stats))
        return f"moving-{len(stats)}"

    monkeypatch.setattr(agent, "default_spec_identity", _never_the_same)
    with pytest.raises(agent.DerivedSpecStale) as excinfo:
        agent.require_fresh_derived_spec("kirocrew-worker", None)
    assert "kept changing" in str(excinfo.value)
    assert str(agent._DEFAULT_SPEC_OBSERVATION_ATTEMPTS) in str(excinfo.value)
    # It gave up rather than looping forever.
    assert len(stats) <= agent._DEFAULT_SPEC_OBSERVATION_ATTEMPTS * 4


def test_the_derive_time_pair_is_also_one_observation(tmp_path, monkeypatch):
    """The same property at the WRITE end, because a torn pair recorded here is what a
    later identity match would accept: the fingerprint is of the bytes this derivation
    mirrored, and the identity is recorded only when a re-stat proves the file held still."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")
    mirrored_bytes = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))

    real_read = agent._installed_default_spec

    def _read_then_move():
        spec = real_read()
        default.write_text(json.dumps(edited), encoding="utf-8")
        return spec

    monkeypatch.setattr(agent, "_installed_default_spec", _read_then_move)
    agent._install_worker_agent()

    # The fingerprint names what was actually mirrored, not what the file now holds.
    assert agent_state.get_mirrored_from("kirocrew-worker") == agent._spec_fingerprint(
        mirrored_bytes
    )
    # And no identity is claimed at all, because none of them describes those bytes.
    assert agent_state.get_mirrored_stat("kirocrew-worker") is None

    # Which leaves the next check in the pessimistic direction: no fast path, and the
    # truthful fingerprint does not match the file, so the mirror is re-derived.
    monkeypatch.setattr(agent, "_installed_default_spec", real_read)
    assert agent._derived_spec_matches_default("kirocrew-worker") is False


def test_the_identity_fast_path_re_stats_after_reading_the_sidecar(tmp_path, monkeypatch):
    """Reading the sidecar is itself a window. The recorded value is evidence about the file
    the FIRST stat described, so without a second stat the fast path can answer "provably
    current" about a default that has already been replaced."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    real_identity = agent.default_spec_identity
    calls: list[str | None] = []

    def _counting():
        answer = real_identity()
        calls.append(answer)
        return answer

    monkeypatch.setattr(agent, "default_spec_identity", _counting)
    assert agent._derived_spec_matches_default("kirocrew-worker") is True
    assert len(calls) == 2, f"the fast path must stat before AND after the sidecar read: {calls}"

    # And a replacement landing during that read is caught rather than accepted.
    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")
    recorded = agent_state.get_mirrored_stat("kirocrew-worker")

    def _read_sidecar_then_move(name):
        default.write_text(json.dumps(edited), encoding="utf-8")
        return recorded

    monkeypatch.setattr(agent, "default_spec_identity", real_identity)
    monkeypatch.setattr(agent_state, "get_mirrored_stat", _read_sidecar_then_move)
    assert agent._derived_spec_matches_default("kirocrew-worker") is False


def test_the_client_closes_the_bracket_after_the_handshake():
    """The second subprocess spawner, and the same placement rule: the AcpClient captures
    the snapshot in ``_spawn`` and must re-verify it in ``_initialize_session`` after the
    ``initialize`` response and BEFORE any session is created, so a session is never
    built on a spec nobody verified."""
    import inspect

    from kiro_crew.acp import client

    spawn_src = inspect.getsource(client.AcpClient._spawn)
    assert (
        "self._derived_spec_snapshot = derived_snapshot" in spawn_src
    ), "the capture half must stay in _spawn -- it is the window's opening edge"

    src = inspect.getsource(client.AcpClient._initialize_session)
    assert (
        "require_unchanged_derived_spec" in src
    ), "the client spawner drives the handshake, so it owns the post-load half"
    before, after = src.split("require_unchanged_derived_spec", 1)
    # The handshake precedes it: the check is worthless before the child has read.
    assert "METHOD_INITIALIZE" in before
    assert "init_resp" in before
    # And no session exists yet when it runs: the only requests above the check are the
    # initialize round trip, and the session id is not recorded until after it.
    assert "METHOD_SESSION_LOAD" not in before
    assert "METHOD_SESSION_LOAD" in after
    assert "self._session_id" not in before
    assert "self._session_id" in after


def test_every_handshake_driving_spawner_closes_the_bracket():
    """Enumeration rather than a list of two files: a module that runs the freshness gate
    AND drives an ``initialize`` of its own is a subprocess spawner, and every one of
    those has the verified-then-changed window. A third one added tomorrow fails here
    instead of shipping the capture half alone."""
    import re as _re

    spawners = []
    closed = []
    for path, body in _package_sources():
        if "require_fresh_derived_spec" not in body:
            continue
        # Drives its own handshake: sends the `initialize` method, however the method
        # name is spelled at that site (a constant in the client, a literal in the
        # runtime).
        if not _re.search(r'METHOD_INITIALIZE|"initialize"', body):
            continue
        spawners.append(path.name)
        if "require_unchanged_derived_spec" in body:
            closed.append(path.name)

    assert sorted(spawners) == [
        "client.py",
        "runtime.py",
    ], f"the set of handshake-driving spawn paths changed: {spawners}"
    assert sorted(closed) == sorted(spawners), (
        f"spawn path(s) capture the snapshot but never close the bracket: "
        f"{sorted(set(spawners) - set(closed))}"
    )


def _client_at_the_handshake(snapshot):
    """An AcpClient positioned exactly at the post-load check, and the methods it sent.

    Built without ``__init__`` on purpose: the property-backed ``backend`` answers ""
    for an unbuilt instance, and everything ``_initialize_session`` touches before the
    check is either set by the handshake itself or supplied here. Anything the check
    were moved BELOW would need attributes this stub does not have, so a test that
    passes here is a test that ran the check early.
    """
    from kiro_crew.acp import client as client_mod

    c = client_mod.AcpClient.__new__(client_mod.AcpClient)
    c._derived_spec_snapshot = snapshot
    sent: list[str] = []

    async def _send_request(method, params):
        sent.append(method)
        return 1

    async def _wait_for_response(req_id, **kwargs):
        return {
            "protocolVersion": 1,
            "agentCapabilities": {},
            "agentInfo": {"name": "kiro", "version": "0.0.0"},
        }

    c._send_request = _send_request  # type: ignore[method-assign]
    c._wait_for_response = _wait_for_response  # type: ignore[method-assign]
    return c, sent


def test_the_client_bracket_aborts_before_a_session_is_created(tmp_path, monkeypatch):
    """The window, driven through the real handshake method: the pre-check passed, the
    default spec then changed, and the child has already read whatever kiro-cli read.
    The refusal must reach the caller as ``AcpError`` -- the type ``ensure_ready``
    handles, and therefore the one that kills the child and drops the half-registered
    session state -- with no session/load or session/new sent."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp import client as client_mod

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["mcpServers"].pop("builder-mcp")
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    c, sent = _client_at_the_handshake(snap)
    with pytest.raises(client_mod.AcpError) as excinfo:
        asyncio.run(c._initialize_session())
    assert "changed during worker load" in str(excinfo.value)
    assert not isinstance(excinfo.value, agent.DerivedSpecStale), (
        "a raw DerivedSpecStale matches none of ensure_ready's handlers, so the child "
        "would stay alive on an unverified spec"
    )
    assert sent == ["initialize"], f"a session was created on an unverified spec: {sent}"


def test_the_client_bracket_judges_the_snapshot_its_own_spawn_captured(monkeypatch):
    """The two halves have to be about the same child. The object the check receives is
    the one ``_spawn`` stored on the instance -- not a fresh capture taken here, which
    would compare the spec against itself and always pass."""
    import asyncio

    from kiro_crew.acp import client as client_mod

    class _Reached(Exception):
        pass

    seen: list[object] = []

    def _record(snapshot, **kwargs):
        seen.append(snapshot)
        raise _Reached

    monkeypatch.setattr(client_mod, "require_unchanged_derived_spec", _record)

    sentinel = object()
    c, sent = _client_at_the_handshake(sentinel)
    with pytest.raises(_Reached):
        asyncio.run(c._initialize_session())
    assert seen and seen[0] is sentinel
    assert sent == ["initialize"]


def test_every_set_mode_send_goes_through_the_one_bracketed_helper():
    """A ``set_mode`` naming an agent is a load of that agent's spec, so it needs the
    same bracket the spawn has -- and there is exactly ONE body that provides it. Both
    session-start paths (create and resume) reach it; no body on either path sends the
    mode method itself. A protocol written twice is two protocols the moment one copy is
    edited, so the pin is on the single helper plus the rule that nothing else sends the
    method. Activation may sit in a body the entry point hands off to -- ``create_session``
    finishes in ``_finish_create_session``, which the late-adoption collector calls too --
    so the walk FOLLOWS the ``self`` calls instead of naming one method."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp import runtime as runtime_mod

    def _tree(fn):
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))

    def _handoffs(fn):
        return {
            n.func.attr
            for n in ast.walk(_tree(fn))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        }

    def _sends_the_mode(fn):
        return any(
            isinstance(n, ast.Call)
            and any(isinstance(a, ast.Name) and a.id == "METHOD_SET_MODE" for a in n.args)
            for n in ast.walk(_tree(fn))
        )

    helper = runtime_mod.AcpRuntime._activate_mode_bracketed
    for entry in (runtime_mod.AcpRuntime.create_session, runtime_mod.AcpRuntime.load_session):
        on_path: set[str] = set()
        pending = [entry.__name__]
        while pending:
            name = pending.pop()
            fn = getattr(runtime_mod.AcpRuntime, name, None)
            if name in on_path or not inspect.isfunction(fn):
                continue
            on_path.add(name)
            pending.extend(_handoffs(fn))
        assert helper.__name__ in on_path, f"{entry.__qualname__} does not activate via the helper"
        senders_on_path = sorted(
            n for n in on_path if _sends_the_mode(getattr(runtime_mod.AcpRuntime, n))
        )
        assert senders_on_path == [
            helper.__name__
        ], f"{entry.__qualname__} sends set_mode outside the helper: {senders_on_path}"

    # The whole module SENDS the method from exactly one place: the helper. The
    # transport's own method-name table (a dict literal mapping the constant to a
    # log label) names it too, so the walk counts CALLS that carry the constant as an
    # argument, not every mention.
    module_tree = ast.parse(inspect.getsource(runtime_mod))
    senders = []
    for node in ast.walk(module_tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for stmt in node.body:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Call) and any(
                    isinstance(arg, ast.Name) and arg.id == "METHOD_SET_MODE" for arg in n.args
                ):
                    senders.append(node.name)
    assert senders == [helper.__name__], senders


def test_the_bracketed_helper_orders_gate_send_and_check_by_consumption_point():
    """Inside the one helper: exactly ONE snapshot per consumed load, and WHERE the spec
    is consumed decides which snapshot the post-check may use.

    Wire-registered (KAS): consumed at ``session/new`` in the payload the projection
    built under its own gate, so the helper takes the PAYLOAD's snapshot and calls no
    gate -- a fresh read would judge the file while the host holds the payload, and a
    revocation between build and activation would pass it. Disk-read (kiro): consumed at
    ``set_mode`` itself, so the helper gates BEFORE the send. The post-check follows the
    send on both paths, by statement position rather than by mention."""
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp import runtime as runtime_mod

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(runtime_mod.AcpRuntime._activate_mode_bracketed))
    )
    fn = tree.body[0]
    body = [s for s in fn.body if not isinstance(s, (ast.Import, ast.ImportFrom, ast.Expr))]

    def _names(stmts):
        return {n.id for s in stmts for n in ast.walk(s) if isinstance(n, ast.Name)}

    split = [s for s in body if isinstance(s, ast.If) and "wire_registered" in _names([s.test])]
    assert len(split) == 1, "the helper must branch on where the spec was consumed"
    wire, own = split[0].body, split[0].orelse
    assert "payload_snapshot" in _names(wire)
    assert "require_fresh_derived_spec" not in _names(
        wire
    ), "a second gate call for one consumed load is the defect, not a safeguard"
    assert "require_fresh_derived_spec" in _names(own), "the disk path consumes at set_mode"

    def _at(target):
        for i, s in enumerate(body):
            if target in _names([s]):
                return i
        raise AssertionError(f"{target} not found")

    assert _at("wire_registered") < _at("METHOD_SET_MODE") < _at("require_unchanged_derived_spec")


def test_a_revocation_between_the_payload_build_and_activation_is_caught(tmp_path, monkeypatch):
    """The window the payload snapshot closes. The definition is registered from spec A, a
    revocation lands, and ``set_mode`` then activates the registered A. A fresh read at
    activation sees B and passes -- so the session runs the grants the revocation removed.
    Comparing against the payload's own snapshot catches it."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.harness import SessionExtras
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.acp.types import METHOD_SET_MODE

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)

    revoked = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    revoked["mcpServers"].pop("builder-mcp")

    async def _payload(agent_name, *, member_dispatch=False):
        # The projection's gate, then the payload built from what it verified -- spec A.
        snapshot = agent.require_fresh_derived_spec(agent_name, str(tmp_path / "wd"))
        # The revocation lands AFTER the definition is registered and BEFORE activation.
        default.write_text(json.dumps(revoked), encoding="utf-8")
        return SessionExtras(custom_agents=[{"id": agent_name}], derived_spec_snapshot=snapshot)

    rt._kas_custom_agents = _payload  # type: ignore[method-assign]

    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew-worker", mcp_servers=[]))

    assert METHOD_SET_MODE in sent, "the activation must have been attempted"
    assert terminated == ["sid-1"], "a session holding a revoked definition must end"
    assert "changed during worker load" in str(excinfo.value)
    # The refusal names the generation the PAYLOAD was built from, which is the whole
    # point: a fresh read at activation would have matched the file and said nothing.
    assert agent.default_spec_fingerprint() not in str(excinfo.value)


# ── a benign concurrent write re-derives and respawns once; a revocation still ends it ──


def _runtime_for_spawn(monkeypatch, attempts_outcomes):
    """A runtime whose ``_spawn_admitted`` plays back the given outcomes in order."""
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.types import ACP_BACKEND_KIRO

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._process = None
    calls: list[int] = []
    outcomes = list(attempts_outcomes)

    async def _spawn_admitted():
        calls.append(len(calls))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome

    rt._spawn_admitted = _spawn_admitted  # type: ignore[method-assign]

    class _Admission:
        active = 0
        queued = 0

        async def acquire(self):
            return 0.0

        def release(self):
            return None

    monkeypatch.setattr(runtime_mod, "_cold_start_admission", lambda: _Admission())
    return rt, calls


def test_a_default_written_during_the_spawn_window_is_re_derived_and_respawned_once(monkeypatch):
    """Fail-closed on a revocation is right; fail-closed on the dashboard's MCP sync
    landing inside a spawn's window is a dispatch that failed for no reason the operator
    did anything wrong. The bracket kills the child either way -- it cannot tell them apart
    -- so the spawn path retries exactly once: the second attempt re-runs the gate, which
    re-derives the mirror from the default as it now stands."""
    import asyncio

    rt, calls = _runtime_for_spawn(
        monkeypatch, [agent.DerivedSpecStale("changed during load"), None]
    )
    asyncio.run(rt.spawn())
    assert calls == [0, 1], "one retry, on the bracket's own refusal"


def test_a_default_that_keeps_moving_across_two_spawns_is_refused(monkeypatch):
    """Never a loop: the exit condition is another process leaving the file alone. A
    second refusal propagates as the runtime's abort type rather than a bare
    ``RuntimeError`` no caller of ``spawn()`` catches."""
    import asyncio

    from kiro_crew.acp.session_handle import AcpRuntimeError

    rt, calls = _runtime_for_spawn(
        monkeypatch,
        [agent.DerivedSpecStale("changed during load"), agent.DerivedSpecStale("still changing")],
    )
    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.spawn())
    assert calls == [0, 1]
    assert "still changing" in str(excinfo.value)


def test_a_mirror_that_cannot_be_re_derived_is_not_retried(monkeypatch):
    """The pre-spawn gate's own refusal arrives as ``AcpRuntimeError`` already, and a
    second attempt would fail the same way for the same reason: the retry is for the
    post-handshake bracket only."""
    import asyncio

    from kiro_crew.acp.session_handle import AcpRuntimeError

    rt, calls = _runtime_for_spawn(monkeypatch, [AcpRuntimeError("could not be re-derived")])
    with pytest.raises(AcpRuntimeError):
        asyncio.run(rt.spawn())
    assert calls == [0], "the gate's refusal is not the bracket's, and is not retried"


class _FakeHandle:
    """The session handle, reduced to what the activation branch touches.

    Patched in rather than constructed, so these tests exercise the RUNTIME's bracket
    around ``set_mode`` and not the handle's own config parsing, served-default lookup or
    drain timing -- each of which has its own tests.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.drained = False
        self.native_context_documents = {}

    def store_session_config(self, resp):
        return None

    async def ensure_served_default(self):
        return None

    async def apply_session_permission_routing(self):
        return None

    def mcp_session_report(self):
        return self

    def begin_session(self, servers):
        return None

    def queued_frame_count(self):
        return 0

    async def drain_init(self, **kwargs):
        self.drained = True


def _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated):
    """A real ``AcpRuntime`` positioned so ``create_session`` reaches the activation branch.

    ``object.__new__`` plus stubbed collaborators, the same shape the KAS projection tests
    in this tree use. Everything the branch itself does is REAL -- the gate calls, the
    ordering, the teardown -- which is what makes these tests fail when the bracket is
    removed from the product.
    """
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.types import ACP_BACKEND_KIRO

    rt = object.__new__(runtime_mod.AcpRuntime)
    rt._initialized = True
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._agent = "kirocrew"
    rt._crew_agent = "kirocrew"
    rt._work_dir = tmp_path / "wd"
    rt._private_memory = False
    rt._native_launch_sources = {}
    rt._mcp_gateway_overlay = None
    rt._agent_capabilities = {}
    rt._session_queues = {}
    rt._session_inits_in_flight = 0
    rt._expect_mcp_reports = False
    rt._pid = 4242  # only the post-activation log line reads it

    resp = {
        "sessionId": "sid-1",
        "modes": {
            "currentModeId": "kirocrew",
            "availableModes": [{"id": "kirocrew"}, {"id": "kirocrew-worker"}],
        },
    }

    async def _send_and_await(method, params, timeout=None):
        sent.append(method)
        return resp

    async def _kas_custom_agents(agent, *, member_dispatch=False):
        # The kiro backend builds no wire surface, so it carries no payload and no payload
        # snapshot -- which is what makes the set_mode line itself the consumed load.
        from kiro_crew.acp.harness import SessionExtras

        return SessionExtras()

    async def _session_work_dir(cwd=None):
        return tmp_path / "ws"

    async def _budget():
        return 1.0

    async def _verify(session_id, resp_, *, override=None):
        return None

    async def _terminate(session_id):
        terminated.append(session_id)

    rt._send_and_await = _send_and_await  # type: ignore[method-assign]
    rt._kas_custom_agents = _kas_custom_agents  # type: ignore[method-assign]
    rt._session_work_dir = _session_work_dir  # type: ignore[method-assign]
    rt._session_start_budget = _budget  # type: ignore[method-assign]
    rt._verify_spawn_agent_active = _verify  # type: ignore[method-assign]
    rt.terminate_session = _terminate  # type: ignore[method-assign]
    rt._finish_session_init = lambda session_id: []  # type: ignore[method-assign]

    monkeypatch.setattr(runtime_mod, "AcpSessionHandle", _FakeHandle)
    monkeypatch.setattr(runtime_mod, "_load_watchdog_settings", lambda crew: None)
    return rt


def test_activating_the_worker_on_an_unrepairable_mirror_never_sends_set_mode(
    tmp_path, monkeypatch
):
    """Fail closed BEFORE the send. Once ``set_mode`` goes out, kiro-cli has loaded the spec
    and booted its servers -- a refusal after that point is a session already running on
    grants the default agent does not have."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.acp.types import METHOD_SESSION_NEW

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    # A mirror that cannot be re-derived, which is the one state the gate refuses.
    (tmp_path / WORKER_AGENT_FILENAME).write_text(
        json.dumps(_stale_worker_spec()), encoding="utf-8"
    )
    agent_state.set_mirrored_from("kirocrew-worker", "stale-fingerprint")
    agent_state.set_mirrored_stat("kirocrew-worker", None)
    monkeypatch.setattr(agent, "rederive_worker_agent", lambda reason: False)

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)

    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew-worker", mcp_servers=[]))

    assert sent == [METHOD_SESSION_NEW], f"set_mode was sent on an unverified spec: {sent}"
    assert terminated == ["sid-1"], "the session must not be left half-registered"
    assert "could not be re-derived" in str(excinfo.value)


def test_a_default_changed_during_set_mode_tears_the_session_down(tmp_path, monkeypatch):
    """The window the pair closes on this path: the pre-check passed, kiro-cli read the spec
    at ``set_mode``, and a revocation landed in between. The session may have activated a
    spec nobody verified, so it is ended rather than left running."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.session_handle import AcpRuntimeError
    from kiro_crew.acp.types import METHOD_SET_MODE

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    edited = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    edited["mcpServers"].pop("builder-mcp")

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)
    real_send = rt._send_and_await

    async def _send_then_revoke(method, params, timeout=None):
        answer = await real_send(method, params, timeout)
        if method == METHOD_SET_MODE:
            # The revocation lands while kiro-cli is loading the spec this activates.
            default.write_text(json.dumps(edited), encoding="utf-8")
        return answer

    rt._send_and_await = _send_then_revoke  # type: ignore[method-assign]

    with pytest.raises(AcpRuntimeError) as excinfo:
        asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew-worker", mcp_servers=[]))

    assert METHOD_SET_MODE in sent, "the send must have happened -- this is the POST-load half"
    assert terminated == ["sid-1"], "a session that may hold an unverified spec must end"
    assert "changed during worker load" in str(excinfo.value)


def test_activating_a_non_derived_agent_costs_no_check(tmp_path, monkeypatch):
    """The scope guard, so ordinary sessions pay nothing. An agent that mirrors nothing has
    no generation to be stale against, and the gate answers that from the name alone --
    without reading the default spec, let alone hashing it."""
    import asyncio

    from kiro_crew.acp.types import METHOD_SET_MODE

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")

    read: list[str] = []
    monkeypatch.setattr(
        agent, "_installed_default_spec", lambda: read.append("read") or _DEFAULT_SPEC_ON_DISK
    )

    sent: list[str] = []
    terminated: list[str] = []
    rt = _runtime_for_create_session(monkeypatch, tmp_path, sent, terminated)

    asyncio.run(rt.create_session(cwd=tmp_path / "ws", agent="kirocrew", mcp_servers=[]))

    assert METHOD_SET_MODE in sent, "a non-derived agent must still be activated"
    assert terminated == []
    assert read == [], "the scope guard must answer before the default spec is read"


# ── the array-backed hosts: the array IS the load, so it carries the snapshot ──


def test_the_array_projection_carries_the_snapshot_it_was_built_from(tmp_path, monkeypatch):
    """Claude/codex/opencode read no spec of their own -- the ``session/new`` array IS
    where they consume the derived spec. So the projection has to hand back the snapshot
    those elements were built from, or the client has a gate and a payload but nothing to
    re-verify once the host has taken it."""
    from kiro_crew import agent_state
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)
    monkeypatch.setattr(session_mcp, "_global_settings", lambda: {})

    projection = session_mcp.session_mcp_projection(
        "kirocrew-worker", work_dir=str(tmp_path / "wd")
    )
    snap = projection.derived_spec_snapshot
    assert snap is not None
    assert snap.identity == agent.default_spec_identity()
    assert snap.fingerprint == agent.default_spec_fingerprint()
    # The elements were built from THAT snapshot's bytes, not from a second read.
    assert {e["name"] for e in projection.servers} >= {"kirocrew-work"}

    # An agent that mirrors nothing has no generation to be stale against.
    assert (
        session_mcp.session_mcp_projection("kirocrew", work_dir=None).derived_spec_snapshot is None
    )


def test_every_array_mirror_passes_the_snapshot_through(monkeypatch):
    """Three mirrors build the wire array from one projection, and each must hand the
    snapshot on rather than dropping it at its own return -- a mirror that forgot would
    leave its host family with no post-consume check while the others had one."""
    import inspect

    from kiro_crew.providers.mirrors import claude_code, codex, opencode

    for module in (claude_code, codex, opencode):
        source = inspect.getsource(module)
        # The return that carries the array carries the snapshot beside it.
        body = source[source.index("session_mcp_projection(") :]
        assert "derived_spec_snapshot=projection.derived_spec_snapshot" in body, module.__name__


def _claude_client_at_session_new(tmp_path, snapshot):
    """A claude-backed AcpClient positioned so ``_initialize_session`` reaches session/new
    with a pre-warmed array and the snapshot it was built from."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp import client as client_mod
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    c = client_mod.AcpClient(
        work_dir=tmp_path, agent="kirocrew-worker", acp_backend=ACP_BACKEND_CLAUDE
    )
    c._resume_session_id = None
    c._session_mcp_cache = []  # warmed on the spawn path; the array itself is not under test
    c._session_mcp_snapshot = snapshot
    sent: list[str] = []

    async def _send_request(method, params):
        sent.append(method)
        return len(sent)

    async def _wait_for_response(rid, timeout=None, *, method="", expected_mcp=None):
        return {"protocolVersion": "1.0", "sessionId": "sess-123"}

    c._send_request = _send_request  # type: ignore[method-assign]
    c._wait_for_response = _wait_for_response  # type: ignore[method-assign]
    c._drain_notifications = AsyncMock()  # type: ignore[method-assign]
    return c, sent


def test_a_revocation_between_the_array_build_and_session_new_ends_the_session(
    tmp_path, monkeypatch
):
    """The gap the array-backed family had: the projection built the array from spec A
    and dropped the snapshot; the client's only post-check judged the kiro-cli
    ``--agent`` read, which a claude child never performs and which is None on this
    backend. A revocation landing after the build and before ``session/new`` therefore
    registered the revoked server with nothing to catch it."""
    import asyncio

    from kiro_crew import agent_state
    from kiro_crew.acp.client import AcpError
    from kiro_crew.acp.types import METHOD_INITIALIZE, METHOD_SESSION_NEW

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    default = tmp_path / AGENT_FILENAME
    default.write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    c, sent = _claude_client_at_session_new(tmp_path, snap)
    real_wait = c._wait_for_response

    async def _wait_then_revoke(rid, timeout=None, *, method="", expected_mcp=None):
        resp = await real_wait(rid, timeout=timeout, method=method, expected_mcp=expected_mcp)
        if method == METHOD_SESSION_NEW:
            revoked = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
            revoked["mcpServers"].pop("builder-mcp")
            default.write_text(json.dumps(revoked), encoding="utf-8")
        return resp

    c._wait_for_response = _wait_then_revoke  # type: ignore[method-assign]

    with pytest.raises(AcpError) as excinfo:
        asyncio.run(c._initialize_session())

    assert sent[:2] == [METHOD_INITIALIZE, METHOD_SESSION_NEW], sent
    assert "changed during worker load" in str(excinfo.value)


def test_an_unchanged_default_lets_the_array_backed_session_proceed(tmp_path, monkeypatch):
    """The ordinary case pays one stat and says nothing."""
    import asyncio

    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    snap = agent.require_fresh_derived_spec("kirocrew-worker", None)

    c, sent = _claude_client_at_session_new(tmp_path, snap)
    asyncio.run(c._initialize_session())
    assert c._session_id == "sess-123"


def test_the_array_snapshot_is_dropped_with_the_array_on_reset(tmp_path):
    """Per-spawn freshness, same as the array: a replacement process must judge its own
    array against its own snapshot, never inherit this one's."""
    from kiro_crew.acp import client as client_mod

    c = client_mod.AcpClient(work_dir=tmp_path, agent="kirocrew-worker")
    c._session_mcp_cache = [{"name": "x"}]
    c._session_mcp_snapshot = object()
    c._reset_state()
    assert c._session_mcp_cache is None
    assert c._session_mcp_snapshot is None


def test_the_session_mcp_projection_reads_nothing_after_the_gate(tmp_path, monkeypatch):
    """ZERO reads, not one read placed carefully. A read taken after the gate returns is a
    SECOND observation of a file the gate already finished with, however tight the sequence
    looks -- and a revocation landing between the two becomes the session's MCP surface as
    though it had been checked. The gate reads and verifies those bytes, so the projection
    is handed the object rather than the path."""
    from kiro_crew import agent_state
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()

    reads: list[str] = []
    real_reader = session_mcp._read_agent_spec

    def _counting(path, **kwargs):
        reads.append(str(path))
        return real_reader(path, **kwargs)

    monkeypatch.setattr(session_mcp, "_read_agent_spec", _counting)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)

    spec = session_mcp._agent_spec_for("kirocrew-worker", str(tmp_path / "wd"))

    assert spec is not None
    assert reads == [], f"the projection re-read the spec after the gate: {reads}"
    # And it is the gate's own object, so the bytes verified and the bytes projected
    # cannot drift apart.
    snap = agent.require_fresh_derived_spec("kirocrew-worker", str(tmp_path / "wd"))
    assert snap is not None and snap.spec == spec

    # Every other agent mirrors nothing, has no snapshot, and is read here as before --
    # otherwise this branch would silently stop serving them.
    reads.clear()
    (tmp_path / "kirocrew.json").exists()
    session_mcp._agent_spec_for("kirocrew", str(tmp_path / "wd"))
    assert reads, "a non-derived agent must still be read from disk"


def test_the_snapshot_reader_takes_no_read_of_its_own(monkeypatch):
    """The exported snapshot is the projection's own answer, not a parallel read that could
    resolve a different spec than the projection ran on."""
    import inspect

    from kiro_crew.acp import session_mcp

    snapshot_src = inspect.getsource(session_mcp.agent_spec_snapshot)
    assert "_agent_spec_for(" in snapshot_src
    assert "_read_agent_spec(" not in snapshot_src
