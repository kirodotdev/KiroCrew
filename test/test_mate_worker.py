# SPDX-License-Identifier: Apache-2.0
"""The crewmate worker: a source-defined base plus the crewmate's own additions.

``rfc-mate-owned-agent-config`` §5.1/§5.4: a crewmate's chat runs on the
conductor, the work it hands off runs on ``kirocrew-mate-worker``, and the
crewmate's own file under ``~/.kiro/mates/agents/`` only ADDS to that worker --
the prompt after the base prompt, servers and skills the base lacks, and the
approvals for those added servers -- with the governance ceiling applied to the
merged result.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import agent, mate_worker
from kiro_crew.agent_files import (
    MATE_WORKER_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    is_kirocrew_default_agent,
)
from kiro_crew.agent_materialization import service_agents
from kiro_crew.config import paths as paths_mod
from kiro_crew.platform import governance

BASE = {
    "name": "kirocrew-mate-worker",
    "prompt": "You are the worker.",
    "tools": ["fs_read", "@kirocrew-core"],
    "allowedTools": ["fs_read", "@kirocrew-core"],
    "resources": ["file://.kiro/steering/**/*.md"],
    "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"]}},
}


def _mate(**over):
    spec = {
        "name": "pixel",
        "prompt": "Follow the release SOP.",
        "tools": ["*"],
        "allowedTools": ["@jira/search", "@kirocrew-core/spawn_run", "execute_bash"],
        "resources": ["skill://release/SKILL.md"],
        "mcpServers": {
            "jira": {"command": "jira-mcp"},
            "kirocrew-core": {"command": "evil"},
        },
    }
    spec.update(over)
    return spec


class TestWorkerAdditions:
    def test_only_what_the_base_lacks_is_an_addition(self):
        adds = mate_worker.worker_additions(BASE, _mate())
        assert list(adds["mcpServers"]) == ["jira"]
        assert adds["resources"] == ["skill://release/SKILL.md"]
        assert adds["prompt"] == "Follow the release SOP."

    def test_approvals_are_taken_only_for_servers_the_file_adds(self):
        adds = mate_worker.worker_additions(BASE, _mate())
        # A built-in grant and a grant on a BASE server are not the file's to give.
        assert adds["allowedTools"] == ["@jira/search"]

    def test_a_copy_of_the_base_adds_nothing(self):
        adds = mate_worker.worker_additions(BASE, dict(BASE, name="pixel"))
        assert not mate_worker.has_additions(adds)


class TestMergeWorkerSpec:
    def test_the_prompt_is_appended_after_the_base_never_replacing_it(self):
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _mate()))
        assert merged["prompt"] == "You are the worker.\n\nFollow the release SOP."

    def test_a_base_server_is_never_redefined_by_the_file(self):
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _mate()))
        assert merged["mcpServers"]["kirocrew-core"] == BASE["mcpServers"]["kirocrew-core"]
        assert merged["mcpServers"]["jira"] == {"command": "jira-mcp"}

    def test_an_added_server_is_mounted_and_its_approvals_carried(self):
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _mate()))
        assert "@jira" in merged["tools"]
        assert "@jira/search" in merged["allowedTools"]
        assert "execute_bash" not in merged["allowedTools"]
        assert merged["resources"] == [*BASE["resources"], "skill://release/SKILL.md"]
        assert merged["name"] == mate_worker.MATE_WORKER_AGENT

    def test_an_added_server_keeps_the_templates_tool_selection(self):
        """A template mounting one tool of a server gives the worker that one tool."""
        mate = _mate(tools=["fs_read", "@notes/read"], mcpServers={"notes": {"command": "n"}})
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, mate))
        assert "@notes/read" in merged["tools"]
        assert "@notes" not in merged["tools"]

    def test_an_added_server_the_template_never_selects_is_not_mounted(self):
        mate = _mate(tools=["fs_read"], mcpServers={"notes": {"command": "n"}})
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, mate))
        assert not [t for t in merged["tools"] if t.startswith("@notes")]

    def test_the_base_is_not_mutated(self):
        before = json.dumps(BASE, sort_keys=True)
        mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _mate()))
        assert json.dumps(BASE, sort_keys=True) == before

    def test_the_ceiling_applies_to_the_merged_spec(self, monkeypatch):
        """An addition can never lift the worker above POLICY ∩ PROFILE."""
        monkeypatch.setattr(
            governance, "may_skip_gate_now", lambda ref: not ref.startswith("@jira")
        )
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _mate()))
        assert "@jira/search" not in merged["allowedTools"]
        # Mounting is not approval: the server is still there, its calls prompt.
        assert "jira" in merged["mcpServers"]


class TestTheProjectionReadsTheCrewmateFromTheSessionRecord:
    def test_the_projected_worker_carries_the_session_members_additions(self, monkeypatch):
        from kiro_crew.acp.harness import kas
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        seen = {}

        def _resolve(base, member_id):
            seen["member"] = member_id
            return mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, _mate()))

        monkeypatch.setattr(mate_worker, "resolve_worker_spec", _resolve)
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda key, **_: ExecutionContext(
                "pixel",
                MemoryStoreRef("member-pixel", "pixel"),
                "template",
                mate_worker.MATE_WORKER_AGENT,
                "persistent",
            ),
        )
        spec = kas.mate_worker_projection(dict(BASE), agent.kiro_agents_dir_path(), "chat-1")
        assert seen["member"] == "pixel"
        assert spec["prompt"].endswith("Follow the release SOP.")

    def test_with_no_record_the_worker_is_refused(self, monkeypatch):
        from kiro_crew.acp.harness import kas

        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda key, **_: None
        )
        with pytest.raises(mate_worker.WorkerKitUnreadable):
            kas.mate_worker_projection(dict(BASE), agent.kiro_agents_dir_path(), "chat-1")

    def test_a_crewmate_whose_own_file_is_absent_is_refused(self, monkeypatch):
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: "")
        monkeypatch.setattr(mate_worker, "_claimed_file", lambda member: "pixel")
        with pytest.raises(mate_worker.WorkerKitUnreadable):
            mate_worker.resolve_worker_spec(dict(BASE), "pixel")

    def test_a_crewmate_on_a_shared_template_keeps_its_revocations(self, monkeypatch):
        from kiro_crew import mate_approval_revocations as revocations

        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: "")
        monkeypatch.setattr(mate_worker, "_claimed_file", lambda member: "")
        monkeypatch.setattr(mate_worker, "_binding_alias", lambda member: "pixel")
        monkeypatch.setattr(revocations, "revoked", lambda fid: ["fs_read"] if fid else [])
        spec = mate_worker.resolve_worker_spec(dict(BASE), "pixel")
        assert "fs_read" not in spec["allowedTools"]

    def test_the_wire_entry_withholds_a_governed_addition(self, monkeypatch, tmp_path):
        """End to end: the KAS custom-agent entry built from the merged spec does
        not auto-approve what the ceiling withholds, and still declares the
        server -- the second, read-time application of the same ceiling."""
        from kiro_crew.acp import kas_agents

        deny = lambda ref: not ref.startswith("@jira")  # noqa: E731
        monkeypatch.setattr(governance, "may_skip_gate_now", deny)
        monkeypatch.setattr(kas_agents, "may_skip_gate_now", deny)
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, _mate()))
        merged["allowedTools"].append("@jira/search")  # a file that dodged the sanitizer
        entry = kas_agents.build_kas_custom_agents(tmp_path, "kirocrew-mate-worker", merged)[0]
        approved = json.dumps(entry.get("permissions"))
        assert "jira" not in approved


class TestMateFileForMember:
    def _home(self, monkeypatch, tmp_path, *, user_level: bool = False):
        from kiro_crew.config import paths

        user, mates = tmp_path / "agents", tmp_path / "mates"
        user.mkdir()
        mates.mkdir()
        (mates / "pixel.json").write_text(json.dumps(_mate()), encoding="utf-8")
        if user_level:
            (user / "pixel.json").write_text(json.dumps({"name": "pixel"}), encoding="utf-8")
        monkeypatch.setattr(paths, "kiro_agents_dir", lambda: user)
        monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
        monkeypatch.setattr(
            "kiro_crew.execution_context.member_config_for_id",
            lambda cfg, member_id: ("pixel", SimpleNamespace(kiro_agent="pixel")),
        )

    def test_a_binding_on_its_own_file_names_it(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        assert mate_worker.mate_file_for_member("pixel", config=object()) == "pixel"

    def test_a_user_level_agent_of_the_same_name_is_not_the_crewmates_file(
        self, monkeypatch, tmp_path
    ):
        self._home(monkeypatch, tmp_path, user_level=True)
        assert mate_worker.mate_file_for_member("pixel", config=object()) == ""

    def test_resolution_merges_that_file(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
        spec = mate_worker.resolve_worker_spec(dict(BASE), "pixel")
        assert "jira" in spec["mcpServers"]
        assert spec["prompt"] == "You are the worker.\n\nFollow the release SOP."

    def test_a_prompt_less_file_adds_no_prompt(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        from kiro_crew.config import paths

        (paths.mate_agents_dir() / "pixel.json").write_text(
            json.dumps(_mate(prompt="")), encoding="utf-8"
        )
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
        spec = mate_worker.resolve_worker_spec(dict(BASE), "pixel")
        assert spec["prompt"] == BASE["prompt"]

    def test_an_approval_revoked_in_approvals_stays_off_though_the_base_grants_it(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import mate_approval_revocations as revocations

        self._home(monkeypatch, tmp_path)
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
        granted = "@kirocrew-core"
        assert granted in mate_worker.resolve_worker_spec(dict(BASE), "pixel")["allowedTools"]

        revocations.record("pixel", [granted], [])
        assert granted not in mate_worker.resolve_worker_spec(dict(BASE), "pixel")["allowedTools"]

        revocations.record("pixel", [], [granted])
        assert granted in mate_worker.resolve_worker_spec(dict(BASE), "pixel")["allowedTools"]

    def test_an_unreadable_crewmate_file_refuses_the_worker(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        from kiro_crew.config import paths

        (paths.mate_agents_dir() / "pixel.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
        with pytest.raises(mate_worker.WorkerKitUnreadable):
            mate_worker.resolve_worker_spec(dict(BASE), "pixel")

    def test_an_unreadable_revocation_record_refuses_the_worker_and_the_edit(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import mate_approval_revocations as revocations

        self._home(monkeypatch, tmp_path)
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
        ledger = revocations._path()
        ledger.write_text("{torn", encoding="utf-8")
        with pytest.raises(mate_worker.WorkerKitUnreadable):
            mate_worker.resolve_worker_spec(dict(BASE), "pixel")
        with pytest.raises(revocations.RevocationsUnreadable):
            revocations.record("pixel", ["@kirocrew-core"], [])
        assert ledger.read_text(encoding="utf-8") == "{torn"


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_mate_worker_agent()
    return json.loads(
        (paths_mod.mate_agents_dir() / MATE_WORKER_AGENT_FILENAME).read_text(encoding="utf-8")
    )


class TestTheWorkerSpecIsSourceDefined:
    def test_it_carries_its_own_name(self, installed):
        assert installed["name"] == "kirocrew-mate-worker"
        assert MATE_WORKER_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_its_base_is_the_default_templates_shape(self, installed):
        from kiro_crew.mate_worker import MATE_WORKER_PROMPT

        template = agent.build_agent_config()
        for key, value in template.items():
            if key in ("name", "description", "prompt"):
                continue
            assert installed.get(key) == value, key
        # The default prompt, then the worker's own principles after it.
        assert installed["prompt"] == f"{template['prompt'].rstrip()}\n\n{MATE_WORKER_PROMPT}"

    def test_its_prompt_says_how_to_report_a_missing_capability(self, installed):
        from kiro_crew.mate_worker import MISSING_CAPABILITY_PREFIX

        assert MISSING_CAPABILITY_PREFIX in installed["prompt"]

    def test_rebuild_installs_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        agent.rebuild_agent_config(clean=True)
        assert (paths_mod.mate_agents_dir() / MATE_WORKER_AGENT_FILENAME).exists()

    def test_its_sessions_keep_the_default_agents_context(self):
        assert is_kirocrew_default_agent("kirocrew-mate-worker")

    def test_it_is_never_offered_as_a_built_from_choice(self):
        from kiro_crew.dashboard.handlers import agent_catalog

        assert MATE_WORKER_AGENT_FILENAME in agent_catalog._BACKGROUND_ONLY_FILES


@pytest.mark.asyncio
async def test_the_kas_session_start_projects_the_merged_worker(tmp_path, monkeypatch):
    """Through the real harness seam: a worker session's ``customAgents`` entry
    carries the crewmate's prompt after the worker's, and its added server."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.types import ACP_BACKEND_KAS
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.mcp_gateway import session_servers as session_servers_mod

    home, user, mates = tmp_path / "home", tmp_path / "agents", tmp_path / "mates"
    for directory in (home, user, mates):
        directory.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (user / MATE_WORKER_AGENT_FILENAME).write_text(json.dumps(BASE), encoding="utf-8")
    (mates / "pixel.json").write_text(json.dumps(_mate()), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent, **_: frozenset()
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_: ExecutionContext(
            "pixel",
            MemoryStoreRef("member-pixel", "pixel"),
            "template",
            mate_worker.MATE_WORKER_AGENT,
            "persistent",
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.member_config_for_id",
        lambda cfg, member_id: ("pixel", SimpleNamespace(kiro_agent="pixel")),
    )
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda cls: object())
    )

    extras = await harness_for(ACP_BACKEND_KAS).session_extras(
        mate_worker.MATE_WORKER_AGENT, work_dir=str(tmp_path / "work"), session_key="chat-1"
    )
    [entry] = extras.custom_agents
    assert entry["prompt"] == "You are the worker.\n\nFollow the release SOP."
    assert "jira" in json.dumps(entry.get("mcpServers"))


class TestPerToolLimitsAreHiddenNotRefused:
    """A tool the saved spec disables is taken out of the worker's tool list.

    The KAS wire drops ``disabledTools`` from a server entry, so the merge
    re-expresses it as ``excludedTools`` (which the wire carries) and strips any
    auto-approval naming the tool; the session still starts.
    """

    def _mate(self):
        return _mate(
            mcpServers={
                "jira": {"command": "jira-mcp", "disabledTools": ["delete_issue"]},
                "kirocrew-core": {"command": "evil", "disabledTools": ["learn_add"]},
            },
            allowedTools=["@jira/search", "@jira/delete_issue"],
        )

    def test_an_added_servers_disabled_tool_is_excluded_and_not_approved(self):
        merged = mate_worker.merge_worker_spec(
            BASE, mate_worker.worker_additions(BASE, self._mate())
        )
        assert "@jira/delete_issue" in merged["excludedTools"]
        assert "@jira/delete_issue" not in merged["allowedTools"]
        assert "@jira/search" in merged["allowedTools"]

    def test_the_files_limit_on_a_base_server_survives_the_base_winning(self):
        merged = mate_worker.merge_worker_spec(
            BASE, mate_worker.worker_additions(BASE, self._mate())
        )
        assert merged["mcpServers"]["kirocrew-core"] == BASE["mcpServers"]["kirocrew-core"]
        assert "@kirocrew-core/learn_add" in merged["excludedTools"]

    def test_the_bases_own_limit_is_honoured_and_its_auto_approval_dropped(self):
        base = json.loads(json.dumps(BASE))
        base["mcpServers"]["kirocrew-core"]["disabledTools"] = ["spawn_run"]
        base["mcpServers"]["kirocrew-core"]["autoApprove"] = ["spawn_run", "learn_list"]
        base["allowedTools"].append("@kirocrew-core/spawn_run")
        merged = mate_worker.merge_worker_spec(base, {})
        assert merged["excludedTools"] == ["@kirocrew-core/spawn_run"]
        assert "@kirocrew-core/spawn_run" not in merged["allowedTools"]
        assert merged["mcpServers"]["kirocrew-core"]["autoApprove"] == ["learn_list"]

    def test_the_kas_wire_entry_hides_the_tool(self, tmp_path):
        from kiro_crew.acp import kas_agents

        merged = mate_worker.merge_worker_spec(
            BASE, mate_worker.worker_additions(BASE, self._mate())
        )
        entry = kas_agents.build_kas_custom_agents(tmp_path, "kirocrew-mate-worker", merged)[0]
        assert "@jira/delete_issue" in entry["excludedTools"]
        assert "@kirocrew-core/learn_add" in entry["excludedTools"]
        assert "delete_issue" not in json.dumps(entry.get("permissions"))


class TestSavedRestrictionsDoNotWiden:
    """A restriction saved anywhere the worker reads stays a restriction."""

    def test_the_files_own_excluded_tools_reach_the_worker(self):
        mate = _mate(
            excludedTools=["execute_bash", "@jira/delete_issue"],
            allowedTools=["@jira/search", "@jira/delete_issue"],
            mcpServers={"jira": {"command": "jira-mcp", "autoApprove": ["delete_issue"]}},
        )
        merged = mate_worker.merge_worker_spec(BASE, mate_worker.worker_additions(BASE, mate))
        assert {"execute_bash", "@jira/delete_issue"} <= set(merged["excludedTools"])
        assert "@jira/delete_issue" not in merged["allowedTools"]
        assert "@jira/search" in merged["allowedTools"]
        assert "delete_issue" not in merged["mcpServers"]["jira"].get("autoApprove", [])

    def test_a_tool_switched_off_on_the_mcp_page_is_excluded(self, monkeypatch):
        rows = [
            SimpleNamespace(name="jira", disabled_tools=["delete_issue"]),
            SimpleNamespace(name="kirocrew-core", disabled_tools=["learn_add"]),
            SimpleNamespace(name="not-mounted", disabled_tools=["anything"]),
        ]
        monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda: rows)
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda _m: "pixel")
        monkeypatch.setattr(
            "kiro_crew.acp.kas_agents.load_agent_spec",
            lambda _d, _n: _mate(prompt="", allowedTools=["@jira/delete_issue"]),
        )
        merged = mate_worker.resolve_worker_spec(BASE, "pixel")
        assert "@jira/delete_issue" in merged["excludedTools"]
        assert "@kirocrew-core/learn_add" in merged["excludedTools"]
        assert "@not-mounted/anything" not in merged["excludedTools"]
        assert "@jira/delete_issue" not in merged["allowedTools"]

    def test_the_switch_holds_for_a_worker_with_no_crewmate_file(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery.list_servers",
            lambda: [SimpleNamespace(name="kirocrew-core", disabled_tools=["learn_add"])],
        )
        merged = mate_worker.resolve_worker_spec(BASE, None)
        assert merged["excludedTools"] == ["@kirocrew-core/learn_add"]

    def test_an_unreadable_mcp_config_adds_nothing(self, monkeypatch):
        def boom():
            raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", boom)
        merged = mate_worker.resolve_worker_spec(BASE, None)
        assert "excludedTools" not in merged


def test_a_template_built_on_the_base_prompt_adds_only_what_follows_it():
    """A template made from the default one opens with the base's own prompt."""
    template = {"prompt": BASE["prompt"] + "\n\n## Release helper\nShip it."}

    additions = mate_worker.worker_additions(BASE, template)
    merged = mate_worker.merge_worker_spec(BASE, additions)

    assert additions["prompt"] == "## Release helper\nShip it."
    assert merged["prompt"].count(BASE["prompt"]) == 1
    assert merged["prompt"].endswith("## Release helper\nShip it.")


def test_the_worker_spec_is_read_by_the_crewmates_member_id(monkeypatch):
    """A crewmate named ``Briar`` has the id ``briar``; its file is found by the id."""
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = SimpleNamespace(agents={"Briar": SimpleNamespace(member_id="briar")})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    seen: list[object] = []
    monkeypatch.setattr(
        mate_worker, "resolve_worker_spec", lambda base, mid: seen.append(mid) or base
    )
    mate_worker.member_worker_spec("Briar")
    mate_worker.member_worker_spec("not-on-the-roster")
    assert seen == ["briar", "not-on-the-roster"]
