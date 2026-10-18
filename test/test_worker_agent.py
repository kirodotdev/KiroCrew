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

The worker's spawn gate and the bracket around a load live in
``test_worker_agent_spawn_gate.py`` and ``test_worker_agent_load_bracket.py``; shared
pins and spec shapes in ``worker_agent_helpers.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from worker_agent_helpers import _accepting_kiro_cli  # noqa: F401
from worker_agent_helpers import (
    _DEFAULT_SPEC_ON_DISK,
    _pin_spec_permissions_cli,
    _stale_worker_spec,
)

from kiro_crew import agent
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    CONDUCTOR_AGENT_FILENAME,
    LEDGER_CONDUCTOR_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    PIPELINE_CONDUCTOR_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
    SECURITY_CONDUCTOR_AGENT_FILENAME,
    WORKER_AGENT_FILENAME,
)
from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions

_INHERITED_PERMISSIONS = {"rules": [{"capability": "web_fetch", "effect": "deny"}]}


# One xdist worker per module, the per-file group its two siblings carry as well:
# their enumeration gates stream src/ (~1,700 files) through ``test/source_corpus.py``
# (``test_worker_agent_spawn_gate.py``, ``test_worker_agent_load_bracket.py``).
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_worker_agent")


@pytest.fixture()
def specs(tmp_path, monkeypatch) -> dict[str, dict[str, Any]]:
    """Install the five related specs into a throwaway agents dir and read them back."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    agent._install_worker_agent()
    agent._install_conductor_agent()
    agent._install_pipeline_conductor_agent()
    agent._install_security_conductor_agent()
    agent._install_ledger_conductor_agent()
    return {
        name: json.loads((tmp_path / name).read_text(encoding="utf-8"))
        for name in (
            WORKER_AGENT_FILENAME,
            CONDUCTOR_AGENT_FILENAME,
            PIPELINE_CONDUCTOR_AGENT_FILENAME,
            SECURITY_CONDUCTOR_AGENT_FILENAME,
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


def test_the_worker_permissions_field_is_gated_on_the_installed_kiro_cli(tmp_path, monkeypatch):
    """Written on an accepting release, withheld on a refusing or unknown one.

    The worker spec gates its ``permissions`` write on the installed kiro-cli,
    sharing the default spec's gate: a kiro-cli whose schema predates the field
    would otherwise refuse the WHOLE spec and fall back to broader default
    grants. ``permissions`` is not a mirrored key, so a withheld write leaves the
    key absent rather than a stale block, and ``allowedTools`` -- the field
    kiro-cli reads -- is untouched either way.
    """
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    inherited_config = agent.build_agent_config()
    inherited_config["permissions"] = _INHERITED_PERMISSIONS
    monkeypatch.setattr(
        agent,
        "build_agent_config",
        lambda: json.loads(json.dumps(inherited_config)),
    )

    def _install() -> dict[str, Any]:
        agent._install_worker_agent()
        return json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))

    _pin_spec_permissions_cli(monkeypatch, "accepts")
    accepting = _install()
    assert accepting.get("permissions"), "an accepting CLI must get the block"
    assert accepting["permissions"] != _INHERITED_PERMISSIONS
    assert accepting["permissions"] == derived_agent_permissions(
        accepting["allowedTools"], WORKER_AGENT_FILENAME
    )
    assert accepting["allowedTools"], "the grant list is never withheld"

    for refusing in ("refuses", "unknown"):
        _pin_spec_permissions_cli(monkeypatch, refusing)
        data = _install()
        assert "permissions" not in data, f"{refusing} CLI must get no block"
        assert data["allowedTools"], "the grant list is never withheld"


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


@pytest.mark.parametrize(
    "filename", [PIPELINE_CONDUCTOR_AGENT_FILENAME, SECURITY_CONDUCTOR_AGENT_FILENAME]
)
def test_a_conductor_with_its_own_store_mounts_the_base_half(specs, filename):
    """The pipeline and security conductors get the work server from the shared
    conductor base: the two conductor verbs auto-approved on ``allowedTools`` and on
    the KAS rule, and the worker half left gated. ``work_brief`` is not theirs:
    neither procedure is dispatched as a ledger item's worker."""
    spec = specs[filename]
    assert "@kirocrew-work" in spec["tools"]
    assert spec["mcpServers"]["kirocrew-work"]["args"][-1] == "mcp-work"
    work = [ref for ref in spec["allowedTools"] if "kirocrew-work" in ref]
    assert work == ["@kirocrew-work/work_ledger_read", "@kirocrew-work/work_ledger_record"]
    match = spec["permissions"]["rules"][0]["match"]
    assert "kirocrew-work/work_ledger_record" in match
    assert "kirocrew-work/work_report" not in match


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
        "@kirocrew-work/work_ledger_rebuild",
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


# ── the worker inherits the default's skill:// mappings ────────────────────

#: The shipped workspace-steering glob ``build_agent_config`` seeds into every spec's
#: ``resources`` (``config/defaults.json``). It is parity with the default agent, so
#: the worker keeps it; the skill mappings are appended after it.
_TEMPLATE_STEERING_GLOB = "file://.kiro/steering/**/*.md"


def _default_with_resources(resources: list) -> dict[str, Any]:
    spec = json.loads(json.dumps(_DEFAULT_SPEC_ON_DISK))
    spec["resources"] = resources
    return spec


def test_the_worker_inherits_a_skill_mapping_from_the_default(tmp_path, monkeypatch):
    """The gap this closes. A skill reaches a generated spec ONLY through a
    ``skill://`` entry in its ``resources``, and the mirror does not copy
    ``resources`` -- so an operator who maps an app skill onto the agent they run
    dispatches workers that see an empty ``skill_search`` scope and no startup
    skills directory. The mapping is APPENDED after the shipped steering glob."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(
        json.dumps(_default_with_resources(["skill://~/.kiro/crew/skills/my-app/SKILL.md"])),
        encoding="utf-8",
    )
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "skill://~/.kiro/crew/skills/my-app/SKILL.md" in worker["resources"]
    # The shipped steering glob is parity with the default and must survive.
    assert _TEMPLATE_STEERING_GLOB in worker["resources"]


def test_only_skill_resources_are_inherited_not_a_default_file_glob(tmp_path, monkeypatch):
    """``skill://`` is the fail-safe boundary on what is INHERITED. A ``file://`` glob
    an operator added to the DEFAULT spec points at their own tree, so it is not
    carried onto the worker -- only its ``skill://`` entries are, appended after the
    worker's own shipped steering glob."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(
        json.dumps(
            _default_with_resources(
                [
                    "file://.kiro/project-steering/**/*.md",
                    "skill://~/.kiro/crew/skills/app-a/SKILL.md",
                ]
            )
        ),
        encoding="utf-8",
    )
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "skill://~/.kiro/crew/skills/app-a/SKILL.md" in worker["resources"]
    assert "file://.kiro/project-steering/**/*.md" not in worker["resources"]
    # The worker's OWN shipped glob stays; the operator's default-spec glob does not.
    assert _TEMPLATE_STEERING_GLOB in worker["resources"]


def test_a_malformed_default_resources_value_adds_no_skill_mapping(tmp_path, monkeypatch):
    """A spec is a hand-editable JSON file, so ``resources`` can hold anything. A
    non-list, or non-string / non-``skill://`` entries, add nothing -- the worker
    keeps only its shipped steering glob, the same fail-safe shape the grant passes
    take."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    for bad in ("not a list", [123, {"x": 1}, "ftp://nope", "skill-not-a-uri"], None):
        (tmp_path / AGENT_FILENAME).write_text(
            json.dumps(_default_with_resources(bad) if bad is not None else _DEFAULT_SPEC_ON_DISK),
            encoding="utf-8",
        )
        agent._install_worker_agent()
        worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
        skills = [r for r in worker.get("resources", []) if str(r).startswith("skill://")]
        assert skills == [], f"{bad!r} should add no skill mapping: {worker.get('resources')}"
        # The shipped glob is untouched by a malformed inherited value.
        assert worker.get("resources") == [_TEMPLATE_STEERING_GLOB], bad


def test_a_skill_on_the_previous_worker_file_is_not_a_source(tmp_path, monkeypatch):
    """The previous worker file is DERIVED and is NOT a source for the mirror: a
    ``skill://`` present only there (not on the default) is not carried forward, so an
    operator who unmaps a skill on the default does not have it resurrected from the
    worker file forever. The one field that crosses from the worker file is a frozen
    ``model``."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    prior = _stale_worker_spec()
    prior["resources"] = ["skill://~/.kiro/crew/skills/only-on-worker-file/SKILL.md"]
    (tmp_path / WORKER_AGENT_FILENAME).write_text(json.dumps(prior), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "skill://~/.kiro/crew/skills/only-on-worker-file/SKILL.md" not in worker.get(
        "resources", []
    )
    # Only the default has no skill, so the worker keeps just its shipped glob.
    assert worker.get("resources") == [_TEMPLATE_STEERING_GLOB]


def test_the_default_skill_mappings_are_merged_without_duplicates(tmp_path, monkeypatch):
    """The default's ``skill://`` entries are appended after config's own steering
    glob, de-duplicated, so a boot that re-runs this writes the SAME list rather than
    growing one (idempotent)."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(
        json.dumps(
            _default_with_resources(
                [
                    "skill://~/.kiro/crew/skills/from-default/SKILL.md",
                    # A duplicate of the default's own entry is not added twice.
                    "skill://~/.kiro/crew/skills/from-default/SKILL.md",
                ]
            )
        ),
        encoding="utf-8",
    )
    agent._install_worker_agent()
    once = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))["resources"]
    assert once == [
        _TEMPLATE_STEERING_GLOB,
        "skill://~/.kiro/crew/skills/from-default/SKILL.md",
    ]
    # Idempotent: the written list is a fixpoint — re-running over it produces it again.
    agent._install_worker_agent()
    twice = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))["resources"]
    assert twice == once


def test_a_worker_with_no_skill_mapping_keeps_only_the_shipped_steering_glob(
    worker_from_installed_default,
):
    """The default-behaviour pin: a worker whose operator mapped no skill carries the
    SAME ``resources`` as the default agent -- the shipped steering glob and nothing
    else -- so nothing changed for anyone who never mapped a skill."""
    worker = worker_from_installed_default()
    assert worker["resources"] == [_TEMPLATE_STEERING_GLOB]


def test_a_skill_mapped_on_the_default_re_derives_the_worker_at_spawn(tmp_path, monkeypatch):
    """A ``skill://`` mapped on the default while the gateway runs reaches the worker at
    its NEXT spawn, not only after a restart. ``apply_skill_mapping`` rewrites
    ``kirocrew.json`` without re-deriving the worker, so the worker file is left stale;
    the spawn gate (`_require_fresh_worker_spec`) must notice and re-derive it. That
    only works if the freshness surface includes the default's ``skill://`` resources,
    so this drives the real gate rather than the fingerprint alone."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    assert agent._derived_spec_matches_default("kirocrew-worker") is True

    # Operator maps a skill on the default (as apply_skill_mapping would) -- no installer
    # re-derives the worker, so its file is now stale.
    mapped = _default_with_resources(["skill://~/.kiro/crew/skills/newly-mapped/SKILL.md"])
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(mapped), encoding="utf-8")
    assert (
        agent._derived_spec_matches_default("kirocrew-worker") is False
    ), "a skill mapped on the default did not move the worker's freshness fingerprint"

    # The spawn gate re-derives the stale mirror in place; the worker now has the skill.
    agent._require_fresh_worker_spec(None)
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "skill://~/.kiro/crew/skills/newly-mapped/SKILL.md" in worker["resources"]
    assert agent._derived_spec_matches_default("kirocrew-worker") is True


def test_a_skill_unmapped_on_the_default_re_derives_the_worker_at_spawn(tmp_path, monkeypatch):
    """The reverse, and the one a stale fingerprint silently gets wrong: unmapping a
    skill on the default must drop it from the worker at the next spawn, not leave it
    live until a restart."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    mapped = _default_with_resources(["skill://~/.kiro/crew/skills/to-remove/SKILL.md"])
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(mapped), encoding="utf-8")
    agent._install_worker_agent()
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "skill://~/.kiro/crew/skills/to-remove/SKILL.md" in worker["resources"]
    assert agent._derived_spec_matches_default("kirocrew-worker") is True

    # Operator unmaps the skill on the default; the worker file is left stale with it.
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    assert (
        agent._derived_spec_matches_default("kirocrew-worker") is False
    ), "unmapping a skill on the default did not move the worker's freshness fingerprint"

    # The spawn gate re-derives; the worker drops the removed skill.
    agent._require_fresh_worker_spec(None)
    worker = json.loads((tmp_path / WORKER_AGENT_FILENAME).read_text(encoding="utf-8"))
    assert "skill://~/.kiro/crew/skills/to-remove/SKILL.md" not in worker.get("resources", [])
    assert agent._derived_spec_matches_default("kirocrew-worker") is True


def test_a_default_file_glob_change_does_not_force_a_worker_rederive(tmp_path, monkeypatch):
    """The converse, so the surface stays minimal: a ``file://`` glob on the default is
    not mirrored onto the worker, so a change to one must NOT move the fingerprint and
    force a pointless re-derive."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    assert agent._derived_spec_matches_default("kirocrew-worker") is True

    changed = _default_with_resources(["file://.kiro/project-steering/**/*.md"])
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(changed), encoding="utf-8")
    assert agent._derived_spec_matches_default("kirocrew-worker") is True


# ── the refresh heals a spec an older build wrote ──────────────────────────


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


def test_the_research_agent_does_not_mirror_the_default_spec_on_disk(tmp_path, monkeypatch):
    """The worker mirror above is the worker's alone. ``kirocrew-research``
    runs an unattended, auto-approved loop, so a server or model the user added to
    ``kirocrew.json`` stays off it; widening that is a governance call, not drift."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_research_agent()
    research = json.loads((tmp_path / RESEARCH_AGENT_FILENAME).read_text(encoding="utf-8"))
    for name in ("builder-mcp", "pdf"):
        assert name not in research["mcpServers"], name
        assert f"@{name}" not in research["tools"], name
        assert f"@{name}" not in research.get("allowedTools", []), name
    assert research["model"] == agent.build_agent_config()["model"]
    assert research["model"] != _DEFAULT_SPEC_ON_DISK["model"]


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
    #
    # The mirror is read ONCE too, first, and both things the writer needs from that file
    # come out of the one parse: whether it is this derivation's to overwrite at all
    # (a shared crew parked on the mirror's filename is refused, not replaced) and the
    # frozen ``model``. It is inside the lock for the same reason the default read
    # is: an attribution made outside it would vouch for a file a dashboard edit could
    # replace before the write.
    assert order == [
        "lock",
        f"read:{WORKER_AGENT_FILENAME}:{True}",
        f"read:{AGENT_FILENAME}:{True}",
        f"write:{True}",
        "unlock",
    ], order
    assert order.count(f"read:{AGENT_FILENAME}:{True}") == 1, order
    assert order.count(f"read:{WORKER_AGENT_FILENAME}:{True}") == 1, order


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
    default instead of reaching the worker until somebody notices.

    ``kirocrew-crew-log`` is the case that exercised it: a read-only opt-in server
    added later, withheld from the mirror with no edit here beyond widening this
    assertion. A worker has no use for another session's crew log -- its own channel
    to its conductor is the work ledger.

    ``kirocrew-debug`` is withheld on the same derivation. Its host-wide views are
    gated in the route to the owner's own dashboard tab, so a worker would reach
    only refusals; granting it would spend the worker's context on tools that
    cannot answer it."""
    assert agent._worker_unassignable_servers() == frozenset(
        {
            "kirocrew-dashboard",
            "kirocrew-crew-log",
            "kirocrew-debug",
            "kirocrew-panel",
        }
    )
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
    allowed.

    The grant is hand-added, so the opt-in is pinned on: the subject is the
    per-verb narrowing of a grant that exists, not whether one may exist."""
    _pin_honour_auto_approve(monkeypatch, True)
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


def _pin_honour_auto_approve(monkeypatch, honour: bool) -> None:
    """Pin ``mcp.honour_auto_approve`` for a grant no spec declares."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.mcp.honour_auto_approve = honour
    monkeypatch.setattr(live, "snapshot", lambda: cfg)


def test_an_auto_approve_naming_only_reading_verbs_survives(tmp_path, monkeypatch):
    """Nothing is narrowed for its own sake: a name that cannot reach an excluded verb
    keeps its exemption.

    The grant here is hand-added to the default spec rather than declared by the
    managed registry, so ``mcp.honour_auto_approve`` is pinned on: the subject is
    the per-verb narrowing, and the undeclared-grant floor would otherwise drop
    the whole list before the narrowing has anything to narrow."""
    _pin_honour_auto_approve(monkeypatch, True)
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
