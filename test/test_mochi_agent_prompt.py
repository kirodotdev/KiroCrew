"""The agent prompt an app renders at runtime actually reaches its agent.

This is the gap the feature closes: an app's agent JSON ships inside the app, so it
can only name paths that exist at packaging time. A prompt that carries USER settings
(a pet's name, a chosen appearance's persona) has to be generated into the data dir,
which means the path can only be stated at runtime. Before this, the packaged prompt
was never attached at all and the agent ran on its one-line manifest description —
so renaming the pet changed nothing and the behaviour rules never took effect.
"""

from __future__ import annotations

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.bridges import _apply_agent_prompt
from kiro_crew.apps.builtins.mochi import soul_loader


class TestApplyAgentPrompt:
    """The framework seam: policy -> materialized agent config."""

    def test_absolute_existing_path_is_pinned(self, tmp_path):
        prompt = tmp_path / "p.md"
        prompt.write_text("hi", encoding="utf-8")
        out = _apply_agent_prompt(
            {"name": "a"}, "a", {"agents": {"a": {"prompt": f"file://{prompt}"}}}, "mochi", tmp_path
        )
        assert out["prompt"] == f"file://{prompt}"

    def test_bare_path_is_accepted_and_normalised_to_a_uri(self, tmp_path):
        prompt = tmp_path / "p.md"
        prompt.write_text("hi", encoding="utf-8")
        out = _apply_agent_prompt(
            {"name": "a"}, "a", {"agents": {"a": {"prompt": str(prompt)}}}, "mochi", tmp_path
        )
        assert out["prompt"] == f"file://{prompt}"

    def test_a_missing_file_is_dropped_not_written_through(self, tmp_path):
        # A prompt path that does not resolve is the exact failure this guards:
        # writing it through would produce an agent that looks configured and is not.
        gone = tmp_path / "nope.md"
        out = _apply_agent_prompt(
            {"name": "a"}, "a", {"agents": {"a": {"prompt": str(gone)}}}, "mochi", tmp_path
        )
        assert "prompt" not in out

    def test_a_relative_path_is_dropped(self, tmp_path):
        out = _apply_agent_prompt(
            {"name": "a"}, "a", {"agents": {"a": {"prompt": "p.md"}}}, "mochi", tmp_path
        )
        assert "prompt" not in out

    @pytest.mark.parametrize("policy", [{}, {"agents": {}}, {"agents": {"a": {}}}])
    def test_no_prompt_in_policy_leaves_the_config_untouched(self, policy, tmp_path):
        cfg = {"name": "a", "tools": ["fs_read"]}
        assert _apply_agent_prompt(dict(cfg), "a", policy, "mochi", tmp_path) == cfg

    def test_another_agents_prompt_is_not_applied(self, tmp_path):
        prompt = tmp_path / "p.md"
        prompt.write_text("hi", encoding="utf-8")
        out = _apply_agent_prompt(
            {"name": "a"}, "a", {"agents": {"b": {"prompt": str(prompt)}}}, "mochi", tmp_path
        )
        assert "prompt" not in out

    def test_the_input_is_not_mutated(self, tmp_path):
        prompt = tmp_path / "p.md"
        prompt.write_text("hi", encoding="utf-8")
        original = {"name": "a"}
        _apply_agent_prompt(
            original, "a", {"agents": {"a": {"prompt": str(prompt)}}}, "mochi", tmp_path
        )
        assert original == {"name": "a"}


class TestRenderAgentPrompt:
    """The app half: identity header + packaged behaviour rules."""

    def test_the_name_leads_the_prompt(self, tmp_path):
        behaviour = tmp_path / "b.md"
        behaviour.write_text("## Behaviour\n\nBe brief.", encoding="utf-8")
        out = soul_loader.render_agent_prompt("Kiro", "A small ghost.", behaviour_path=behaviour)
        # First line, not buried: an identity stated after thousands of words of
        # rules competes with them instead of framing them.
        assert out.splitlines()[0] == "# You are Kiro"

    def test_a_renamed_pet_is_told_not_to_revert(self, tmp_path):
        behaviour = tmp_path / "b.md"
        behaviour.write_text("rules", encoding="utf-8")
        out = soul_loader.render_agent_prompt("Bao", "persona", behaviour_path=behaviour)
        assert "Your name is **Bao**" in out
        assert "never fall back to a previous one" in out
        # The default must not leak in alongside the chosen name.
        assert "Mochi" not in out

    def test_the_behaviour_document_is_attached(self, tmp_path):
        behaviour = tmp_path / "b.md"
        behaviour.write_text("## Watch List\n\nrules here", encoding="utf-8")
        out = soul_loader.render_agent_prompt("Kiro", "persona", behaviour_path=behaviour)
        assert "## Watch List" in out
        assert "rules here" in out

    def test_an_empty_name_falls_back_to_the_default(self, tmp_path):
        behaviour = tmp_path / "b.md"
        behaviour.write_text("rules", encoding="utf-8")
        for name in ("", "   "):
            out = soul_loader.render_agent_prompt(name, "persona", behaviour_path=behaviour)
            assert f"# You are {soul_loader.DEFAULT_PET_NAME}" in out

    def test_an_unreadable_behaviour_doc_degrades_to_identity_only(self, tmp_path):
        # A pet with a persona and no rulebook is still a working companion; raising
        # here would take the whole agent down at registration instead.
        out = soul_loader.render_agent_prompt(
            "Kiro", "A small ghost.", behaviour_path=tmp_path / "missing.md"
        )
        assert "# You are Kiro" in out
        assert "A small ghost." in out

    def test_the_packaged_behaviour_doc_is_the_default_source(self):
        # Guards the wiring, not the content: this file being detached from every
        # agent is precisely the bug that shipped.
        assert soul_loader.BEHAVIOUR_PROMPT.is_file()
        out = soul_loader.render_agent_prompt("Kiro", "persona")
        assert "## Safety" in out


class TestWriteAgentPrompt:
    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits only")
    def test_it_writes_owner_only_and_returns_the_path(self, tmp_path):
        path = soul_loader.write_agent_prompt(tmp_path, "Kiro", "persona")
        assert path == soul_loader.rendered_prompt_path(tmp_path)
        assert path.is_file()
        assert "# You are Kiro" in path.read_text(encoding="utf-8")
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_a_rename_replaces_the_file_rather_than_appending(self, tmp_path):
        soul_loader.write_agent_prompt(tmp_path, "Kiro", "persona")
        path = soul_loader.write_agent_prompt(tmp_path, "Bao", "persona")
        text = path.read_text(encoding="utf-8")
        assert "# You are Bao" in text
        assert "Kiro" not in text


class TestPolicyCarriesThePrompt:
    def test_build_policy_pins_a_prompt_for_every_agent(self, tmp_path):
        from kiro_crew.apps.builtins.mochi.agent_policy import BG_AGENT, CHAT_AGENT, build_policy

        policy = build_policy({}, tmp_path)
        assert (
            policy["agents"][CHAT_AGENT]["prompt"]
            == f"file://{soul_loader.rendered_prompt_path(tmp_path)}"
        )
        assert (
            policy["agents"][BG_AGENT]["prompt"]
            == f"file://{soul_loader.rendered_bg_prompt_path(tmp_path)}"
        )

    def test_the_two_agents_do_not_share_one_prompt(self, tmp_path):
        """The background agent is a spawned subagent with a different contract.

        Handing it the chat document is how it came to be told it could spawn
        subagents, save lessons, and answer in plain text — none of which it can
        do. A single shared path would silently restore that.
        """
        from kiro_crew.apps.builtins.mochi.agent_policy import BG_AGENT, CHAT_AGENT, build_policy

        policy = build_policy({}, tmp_path)
        assert policy["agents"][CHAT_AGENT]["prompt"] != policy["agents"][BG_AGENT]["prompt"]

    def test_without_a_data_dir_no_prompt_is_claimed(self):
        from kiro_crew.apps.builtins.mochi.agent_policy import CHAT_AGENT, build_policy

        policy = build_policy({})
        assert "prompt" not in policy["agents"][CHAT_AGENT]
