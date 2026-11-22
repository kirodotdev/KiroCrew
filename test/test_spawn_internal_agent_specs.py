"""Kiro Crew's own generated agent specs are never offered, or accepted, as sub-agents.

A side turn publishes ``~/.kiro/agents/<agent>--readonly.json``
(``dashboard.side_readonly_spec``) because that directory is the only place
kiro-cli loads a spec from. Every spawn roster read that directory unfiltered,
so ``spawn_run``'s "Valid names right now", ``spawn_list``'s "Available agents"
and the unknown-agent refusal all offered the read-only spec, and the gate
accepted it. The child's kiro-cli then refused the mode ("Agent mode
'<agent>--readonly' is not available on this session") and told the user to run
``kirocrew setup --agent-only``, which does not write that file.

Every test here publishes the spec through the REAL publisher into a temporary
registry and reads it back through the REAL ``list_agents`` -- no roster is
mocked -- so what is filtered is exactly the file a side turn writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery
from kiro_crew import subagent as sa
from kiro_crew.dashboard import side_readonly_spec as srs
from kiro_crew.mcp_tools import spawn as spawn_tools

_BASE = {"name": "scout", "description": "Scouts the repo", "allowedTools": ["fs_read"]}


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary kiro agent registry holding ``scout`` and its published read-only spec."""
    agents = tmp_path / "agents"
    agents.mkdir()
    # The publisher writes through ``agent.KIRO_AGENTS_DIR``; discovery reads
    # through its own hook. Both point at the one temporary directory.
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(srs, "_refresh_materialized_snapshot", lambda: None)
    (agents / "scout.json").write_text(json.dumps(_BASE), encoding="utf-8")
    published = srs.publish_readonly_spec("scout")
    assert published.name == "scout--readonly"
    assert (agents / "scout--readonly.json").is_file(), "fixture premise: the real file"
    return agents


def _names() -> set[str]:
    return {a.name for a in agent_discovery.list_agents()}


def _configure_default_agent(name: str) -> None:
    """Write ``agent.default_agent`` into this test's own data home config."""
    from kiro_crew.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": {"default_agent": name}}), encoding="utf-8")


class TestThePredicate:
    def test_the_published_spec_is_internal_and_its_base_is_not(self, registry: Path) -> None:
        rows = {a.name: a for a in agent_discovery.list_agents()}
        # Premise: discovery DOES list the file, so the filter is what hides it.
        assert set(rows) >= {"scout", "scout--readonly"}
        assert agent_discovery.is_internal_agent_spec(rows["scout--readonly"])
        assert not agent_discovery.is_internal_agent_spec(rows["scout"])

    def test_a_users_own_agent_ending_in_readonly_is_not_internal(self, registry: Path) -> None:
        """The owner marker decides, not the name: a hand-authored spec that is
        merely called ``<x>--readonly`` is the user's agent and stays spawnable."""
        (registry / "mine--readonly.json").write_text(
            json.dumps({"name": "mine--readonly", "description": "my own read-only helper"}),
            encoding="utf-8",
        )
        rows = {a.name: a for a in agent_discovery.list_agents()}
        assert not agent_discovery.is_internal_agent_spec(rows["mine--readonly"])
        assert sa._validate_agent("mine--readonly") == ("mine--readonly", "", "")

    def test_a_skill_view_alias_row_is_internal(self) -> None:
        alias = SimpleNamespace(
            name="kirocrew-skill-view-abc", filename="kirocrew-skill-view-abc.json"
        )
        assert agent_discovery.is_internal_agent_spec(alias)

    def test_a_partial_row_is_answered_not_raised(self) -> None:
        """Rosters' tests and edition seams hand in rows without every field."""
        assert not agent_discovery.is_internal_agent_spec(SimpleNamespace(name="scout"))
        assert not agent_discovery.is_internal_agent_spec(SimpleNamespace(name=None))


class TestRostersLeaveItOut:
    def test_the_spawn_run_parameter_roster(self, registry: Path) -> None:
        assert "scout--readonly" in _names(), "premise: discovery lists it"
        assert spawn_tools._agent_roster_hint() == " Valid names right now: scout."

    def test_the_spawn_list_listing(self, registry: Path) -> None:
        with patch.object(spawn_tools.mcp_core, "_get", return_value={"agents": []}):
            out = spawn_tools.spawn_list("spawn_list", {})
        assert out.endswith("\nAvailable agents: scout")
        assert "--readonly" not in out

    def test_the_unknown_agent_refusal(self, registry: Path) -> None:
        name, err, code = sa._validate_agent("nope")
        assert (name, code) == ("", sa.AGENT_NOT_FOUND_CODE)
        assert err == "agent 'nope' not found; available: scout"


class TestTheGateRefusesIt:
    def test_naming_the_readonly_spec_is_refused_with_its_own_code(self, registry: Path) -> None:
        name, err, code = sa._validate_agent("scout--readonly")
        assert name == ""
        assert code == sa.AGENT_INTERNAL_CODE
        assert err == (
            "agent 'scout--readonly' is the read-only spec Kiro Crew derives from 'scout' "
            "for side replies, not a sub-agent; name 'scout' to spawn that agent; "
            "available: scout"
        )

    def test_a_base_that_is_not_offered_is_not_suggested(self, registry: Path) -> None:
        """The host default is reached by omitting ``agent``; the refusal does not
        tell the caller to name it."""
        (registry / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "description": "host"}), encoding="utf-8"
        )
        srs.publish_readonly_spec("kirocrew")
        _, err, code = sa._validate_agent("kirocrew--readonly")
        assert code == sa.AGENT_INTERNAL_CODE
        assert "name 'kirocrew'" not in err
        assert err.endswith("; available: scout")

    def test_a_project_agent_declaring_the_name_still_wins(self, registry: Path) -> None:
        """kiro-cli resolves the project scope first, so a project agent of that
        name is what would run: it is the user's and is accepted."""
        with patch.object(
            sa, "cached_project_agent_names", return_value=frozenset({"scout--readonly"})
        ):
            assert sa._validate_agent("scout--readonly", "/some/project") == (
                "scout--readonly",
                "",
                "",
            )

    def test_the_wave_stops_on_the_internal_code(self) -> None:
        """``spawn_run`` stops re-posting a name the gateway refused as internal,
        exactly as it does for an unknown one."""
        assert spawn_tools._is_unknown_agent_refusal(
            {"code": sa.AGENT_INTERNAL_CODE}, "scout--readonly"
        )

    def test_an_app_does_not_own_a_spec_derived_from_its_agent(self, registry: Path) -> None:
        """``<app>--<agent>--readonly`` shares the app's filename prefix; sharing
        it does not make the side turn's spec one of the app's agents."""
        (registry / "myapp--helper.json").write_text(
            json.dumps({"name": "myapp--helper", "description": "app agent"}), encoding="utf-8"
        )
        srs.publish_readonly_spec("myapp--helper")
        assert sa._validate_app_agent_ownership("myapp--helper", "myapp") == ""
        refusal = sa._validate_app_agent_ownership("myapp--helper--readonly", "myapp")
        assert "may only spawn its OWN agents" in refusal

    def test_the_app_spawn_sdk_refuses_it_before_the_host(self, registry: Path) -> None:
        """The SDK path marks its agent prevalidated, so the gate above never sees
        it: the SDK's own ownership set must leave the derived spec out too."""
        import asyncio

        from kiro_crew.apps.spawn_sdk import SpawnError, SpawnSDK, build_spawn_impl

        (registry / "myapp--helper.json").write_text(
            json.dumps({"name": "myapp--helper", "description": "app agent"}), encoding="utf-8"
        )
        srs.publish_readonly_spec("myapp--helper")
        calls: list[str] = []

        class _Manager:
            def spawn(self, task: str, **kwargs: object) -> object:
                calls.append(str(kwargs.get("agent")))
                return SimpleNamespace(id="sub-1", error="")

        sdk = SpawnSDK("myapp", build_spawn_impl(_Manager()))
        with patch("kiro_crew.apps.spawn_sdk._audit_spawn_denied"):
            with pytest.raises(SpawnError, match="only spawn its OWN"):
                asyncio.run(sdk.run("do a thing", "myapp--helper--readonly"))
        assert calls == [], "the derived spec must never reach the manager"


class TestTheModeRefusalExplanation:
    def test_an_ordinary_agent_keeps_the_setup_remedy(self) -> None:
        cause, remedy = srs.unavailable_mode_explanation("ops")
        assert cause == "its ~/.kiro/agents/ops.json is likely missing."
        assert remedy == "Run `kirocrew setup --agent-only` to materialize the agent config."

    def test_a_missing_configured_default_names_the_setting_not_setup(self, registry: Path) -> None:
        """Every plain session starts from ``agent.default_agent``. Setup writes only
        Kiro Crew's own specs and never edits the setting, so telling the user to run
        it leaves every plain session refused; the remedy is the setting."""
        _configure_default_agent("default")
        cause, remedy = srs.unavailable_mode_explanation("default")
        shown = srs._home_relative(registry / "default.json")
        assert cause == (
            f"agent.default_agent names 'default', which has no spec at {shown}, "
            "and setup writes only Kiro Crew's own agents."
        )
        assert remedy == (
            "Run `kirocrew config set agent.default_agent kirocrew`, or pick another "
            "default in the chat agent picker, then start a new session."
        )
        assert "kirocrew setup" not in cause + remedy

    def test_a_missing_managed_default_keeps_the_setup_remedy(self, registry: Path) -> None:
        """``kirocrew.json`` is a spec setup does write, so setup stays the answer."""
        _configure_default_agent("kirocrew")
        cause, remedy = srs.unavailable_mode_explanation("kirocrew")
        assert cause == "its ~/.kiro/agents/kirocrew.json is likely missing."
        assert remedy == "Run `kirocrew setup --agent-only` to materialize the agent config."

    def test_an_agent_other_than_the_configured_default_keeps_its_answer(
        self, registry: Path
    ) -> None:
        _configure_default_agent("default")
        _, remedy = srs.unavailable_mode_explanation("ops")
        assert remedy == "Run `kirocrew setup --agent-only` to materialize the agent config."
        (registry / "default.json").write_text(json.dumps({"name": "default"}), encoding="utf-8")
        # A default whose file IS present was refused by kiro-cli's parser: the
        # setting is not what is wrong, so it gets the parse check.
        cause, remedy = srs.unavailable_mode_explanation("default")
        assert "agent.default_agent" not in cause + remedy
        assert "kiro-cli agent validate" in remedy

    def test_a_bots_agent_names_the_dashboard_not_setup(
        self, registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Bot chats on the mate conductor, which lives in ``mates/agents/``:
        kiro-cli does not list that folder and setup does not write it, so the
        setup remedy cannot fix a Bot."""
        from kiro_crew.config import paths as paths_mod

        mates = tmp_path / "mates" / "agents"
        mates.mkdir(parents=True)
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: registry)
        monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: mates)
        (mates / "kirocrew-mate-conductor.json").write_text("{}", encoding="utf-8")
        (mates / "helper.json").write_text("{}", encoding="utf-8")
        for agent in ("kirocrew-mate-conductor", "kirocrew-mate-worker", "helper"):
            cause, remedy = srs.unavailable_mode_explanation(agent)
            assert cause == (
                f"{agent!r} is a Bot's agent, which Kiro Crew keeps in "
                f"{srs._home_relative(mates)}/, not in the agents folder kiro-cli lists, "
                "and `kirocrew setup --agent-only` does not write it."
            )
            assert remedy == "Chat with the Bot from the dashboard's Bots page."
        # A name with no Bot file keeps the ordinary answer.
        _, remedy = srs.unavailable_mode_explanation("ops")
        assert remedy == "Run `kirocrew setup --agent-only` to materialize the agent config."

    def test_a_published_spec_is_explained_and_names_its_base(self, registry: Path) -> None:
        cause, remedy = srs.unavailable_mode_explanation("scout--readonly")
        assert "kirocrew setup --agent-only" not in cause + remedy
        assert "derives from 'scout'" in cause
        assert remedy == (
            "A new session starts a kiro-cli that lists it. It is not a sub-agent: to spawn "
            "its source agent, name 'scout', or omit 'agent' when that is the default agent."
        )

    def test_the_host_default_base_is_given_a_remedy_it_can_follow(self, registry: Path) -> None:
        """The common derived spec is ``kirocrew--readonly``. The host default is
        reached by omitting ``agent``, and a new session is what re-lists the
        spec, so the remedy must say both rather than only "name 'kirocrew'"."""
        (registry / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "description": "host"}), encoding="utf-8"
        )
        srs.publish_readonly_spec("kirocrew")
        _, remedy = srs.unavailable_mode_explanation("kirocrew--readonly")
        assert remedy.startswith("A new session starts a kiro-cli that lists it.")
        assert "omit 'agent' when that is the default agent" in remedy

    def test_a_published_project_scope_spec_is_explained_too(self, registry: Path) -> None:
        derived = srs.readonly_agent_name("scout", "/repo")
        spec = srs.derive_readonly_spec(_BASE, base_name="scout", source_id="/repo")
        (registry / f"{derived}.json").write_text(json.dumps(spec), encoding="utf-8")
        cause, _ = srs.unavailable_mode_explanation(derived)
        assert "derives from 'scout'" in cause

    def test_the_name_alone_does_not_decide(self, registry: Path) -> None:
        """A user's own ``mine--readonly`` (no owner marker) and a derived-shaped
        name with no file both keep the ordinary answer: the wording is keyed on
        the marker, so it never tells a user their own agent is Kiro Crew's."""
        (registry / "mine--readonly.json").write_text(
            json.dumps({"name": "mine--readonly", "description": "mine"}), encoding="utf-8"
        )
        ordinary = "Run `kirocrew setup --agent-only` to materialize the agent config."
        cause, remedy = srs.unavailable_mode_explanation("mine--readonly")
        # Present, so it gets the present-file answer, not the read-only one.
        assert "derives from" not in cause
        assert "kiro-cli agent validate" in remedy
        assert srs.unavailable_mode_explanation("ghost--readonly")[1] == ordinary

    def test_a_present_file_kiro_cli_did_not_list_points_at_validate(self, registry: Path) -> None:
        """A file that exists was found and refused (or written after kiro-cli
        started), so "likely missing" sends the user hunting for nothing."""
        (registry / "ops.json").write_text(json.dumps({"name": "ops"}), encoding="utf-8")
        cause, remedy = srs.unavailable_mode_explanation("ops")
        shown = srs._home_relative(registry / "ops.json")
        assert "likely missing" not in cause
        assert cause.startswith(f"its {shown} is present, but kiro-cli did not")
        assert (
            f"`kiro-cli agent validate --path {srs._shell_quoted(registry / 'ops.json')}`" in remedy
        )
        assert "kirocrew setup" not in remedy

    #: The reproduced shape: a narrow agent with a ``permissions`` block and no
    #: ``allowedTools``, which kiro-cli lists as "(v3 format)" and its v2 engine skips.
    _V3_SHAPED = {
        "name": "narrow",
        "tools": ["fs_read", "fs_write", "execute_bash"],
        "permissions": {"rules": [{"capability": "read", "effect": "allow"}]},
    }

    @pytest.fixture
    def kas_offered(self, monkeypatch):
        from kiro_crew import kiro_on_kas_notice

        offered = {"value": True}
        monkeypatch.setattr(kiro_on_kas_notice, "kas_backend_offered", lambda: offered["value"])
        return offered

    def test_a_v3_shaped_file_with_no_kas_offered_names_no_backend(
        self, registry: Path, kas_offered: dict
    ) -> None:
        """A policy that denies ``kas`` keeps the child on v2 and refuses a ``kas``
        spawn too, so the remedy must not send the caller there."""
        kas_offered["value"] = False
        (registry / "narrow.json").write_text(json.dumps(self._V3_SHAPED), encoding="utf-8")
        cause, remedy = srs.unavailable_mode_explanation("narrow", kiro_cli_engine=True)
        assert "(v3 format)" in cause
        assert remedy == "No backend offered on this host runs kiro-cli's v3 engine."
        assert "'kas'" not in remedy and "allowedTools" not in remedy

    @pytest.mark.parametrize(
        "extra",
        [{}, {"excludedTools": ["@kirocrew-core"]}, {"includePowers": True}],
        ids=["permissions", "excludedTools", "includePowers"],
    )
    def test_a_v3_shaped_file_on_the_kiro_cli_engine_points_at_kas(
        self, registry: Path, extra: dict, kas_offered: dict
    ) -> None:
        """``validate`` passes such a file and a new session lists it no better, so
        the answer points at kiro-cli's own listing and at the backend that serves it."""
        spec = {**self._V3_SHAPED, **extra}
        if extra:
            spec.pop("permissions")
        (registry / "narrow.json").write_text(json.dumps(spec), encoding="utf-8")
        cause, remedy = srs.unavailable_mode_explanation("narrow", kiro_cli_engine=True)
        assert "`kiro-cli agent list` marks it (v3 format)" in cause
        assert "kiro-cli's v2 engine" in cause
        assert remedy == "Spawn it with backend 'kas', which runs kiro-cli's v3 engine."
        assert "allowedTools" not in cause + remedy
        assert "agent validate" not in remedy

    @pytest.mark.parametrize("v2_key", ["allowedTools", "toolsSettings"])
    def test_a_file_with_a_v2_field_keeps_the_validate_answer(
        self, registry: Path, v2_key: str
    ) -> None:
        spec = {**self._V3_SHAPED, v2_key: ["fs_read"] if v2_key == "allowedTools" else {}}
        (registry / "narrow.json").write_text(json.dumps(spec), encoding="utf-8")
        cause, remedy = srs.unavailable_mode_explanation("narrow", kiro_cli_engine=True)
        assert "v3 format" not in cause
        assert "kiro-cli agent validate" in remedy

    def test_the_same_file_on_another_harness_keeps_the_validate_answer(
        self, registry: Path
    ) -> None:
        """Only the Kiro CLI harness runs the v2 engine, so only it names that format."""
        (registry / "narrow.json").write_text(json.dumps(self._V3_SHAPED), encoding="utf-8")
        cause, remedy = srs.unavailable_mode_explanation("narrow")
        assert "v3 format" not in cause
        assert "kiro-cli agent validate" in remedy

    def test_the_alias_answer_wins_over_the_format_answer(self, registry: Path) -> None:
        spec = {**self._V3_SHAPED, "includeMcpJson": True, "useLegacyMcpJson": True}
        (registry / "narrow.json").write_text(json.dumps(spec), encoding="utf-8")
        cause, _ = srs.unavailable_mode_explanation("narrow", kiro_cli_engine=True)
        assert "deprecated alias" in cause

    def test_the_shown_path_is_the_probed_file(self, registry: Path, monkeypatch) -> None:
        """``KIRO_HOME`` moves the agents directory; the answer names that file."""
        (registry / "ops.json").write_text(json.dumps({"name": "ops"}), encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: registry.parent))
        cause, remedy = srs.unavailable_mode_explanation("ops")
        assert f"its ~/{registry.name}/ops.json is present" in cause
        assert "~/.kiro/agents/ops.json" not in cause + remedy

    def test_a_path_with_a_space_stays_one_shell_argument(self) -> None:
        import shlex

        quoted = srs._shell_quoted(Path("/opt/Kiro Home/agents/ops.json"))
        if os.name == "nt":
            assert quoted.startswith('"') and quoted.endswith('"')
        else:
            assert shlex.split(quoted) == ["/opt/Kiro Home/agents/ops.json"]

    def test_a_path_outside_home_is_shown_absolute(self) -> None:
        outside = Path("/opt/kiro/agents/ops.json")
        assert srs._home_relative(outside) == str(outside)

    def test_a_key_and_its_alias_together_are_named(self, registry: Path) -> None:
        """Python's json reads both keys; kiro-cli's strict parser sees one field
        twice and drops the agent. The answer names both keys and the fix."""
        (registry / "Devops-Engineer.json").write_text(
            json.dumps(
                {"name": "Devops-Engineer", "includeMcpJson": True, "useLegacyMcpJson": True}
            ),
            encoding="utf-8",
        )
        cause, remedy = srs.unavailable_mode_explanation("Devops-Engineer")
        assert "'includeMcpJson' and 'useLegacyMcpJson'" in cause
        assert "duplicate field" in cause
        assert remedy.startswith("Remove 'useLegacyMcpJson' and keep 'includeMcpJson'")

    def test_one_of_the_two_keys_alone_is_not_flagged(self, registry: Path) -> None:
        (registry / "ops.json").write_text(
            json.dumps({"name": "ops", "useLegacyMcpJson": True}), encoding="utf-8"
        )
        cause, _ = srs.unavailable_mode_explanation("ops")
        assert "duplicate field" not in cause

    def test_a_name_that_is_not_one_path_component_keeps_the_missing_answer(
        self, registry: Path
    ) -> None:
        (registry / "ops.json").write_text(json.dumps({"name": "ops"}), encoding="utf-8")
        cause, _ = srs.unavailable_mode_explanation("../agents/ops")
        assert cause.endswith("is likely missing.")

    def test_the_name_inverse_matches_both_derived_shapes(self) -> None:
        assert srs.readonly_base_name(srs.readonly_agent_name("scout")) == "scout"
        assert srs.readonly_base_name(srs.readonly_agent_name("scout", "/repo")) == "scout"
        assert srs.readonly_base_name("scout") is None
        assert srs.readonly_base_name("scout--readonly-XYZ") is None
