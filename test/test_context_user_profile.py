"""Tests for the [USER PROFILE] session-context block.

The block is built from onboarding answers (dashboard.user_role /
dashboard.user_technical_level) by ``_build_user_profile_section`` and
injected by ``build_session_context`` for ALL agents. When neither field
is set the block must be entirely absent so un-profiled installs see
byte-identical context.
"""

from __future__ import annotations

import json

from kiro_crew.config.loader import config_path
from kiro_crew.context import ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


def _seed_profile(role: str = "", tech: str = "") -> None:
    """Write a config.json with profile fields into the test-isolated home.

    conftest pins KIROCREW_HOME to a per-test tmp dir, so config_path()
    resolves inside it and KiroCrewConfig.load() picks this file up.
    """
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"dashboard": {"user_role": role, "user_technical_level": tech}}),
        encoding="utf-8",
    )


def _builder(tmp_path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


class TestUserProfileSection:
    def test_absent_when_unset(self, tmp_path):
        """No profile answers → no block at all (not even an empty header)."""
        _seed_profile("", "")
        ctx = _builder(tmp_path).build_session_context()
        assert "[USER PROFILE]" not in ctx

    def test_role_and_tech_level_render(self, tmp_path):
        _seed_profile("designer", "somewhat-technical")
        ctx = _builder(tmp_path).build_session_context()
        assert "[USER PROFILE]" in ctx
        assert "UX / product designer" in ctx
        assert "somewhat technical" in ctx
        assert "[End of user profile]" in ctx

    def test_role_only(self, tmp_path):
        _seed_profile(role="developer")
        ctx = _builder(tmp_path).build_session_context()
        assert "The user is a software developer." in ctx
        assert "Technical comfort:" not in ctx

    def test_tech_level_only(self, tmp_path):
        _seed_profile(tech="non-technical")
        ctx = _builder(tmp_path).build_session_context()
        assert "Technical comfort: not technical" in ctx
        assert "The user is " not in ctx  # no role sentence rendered

    def test_other_role_contributes_nothing(self, tmp_path):
        """'other' has no useful description; alone it must not emit a block."""
        _seed_profile(role="other")
        ctx = _builder(tmp_path).build_session_context()
        assert "[USER PROFILE]" not in ctx

    def test_unknown_slug_treated_as_unset(self, tmp_path):
        """Hand-edited config with an invalid slug must not leak raw slugs."""
        _seed_profile(role="hacker", tech="wizard")
        ctx = _builder(tmp_path).build_session_context()
        assert "[USER PROFILE]" not in ctx
        assert "hacker" not in ctx

    def test_calibration_instruction_present(self, tmp_path):
        """The block must instruct HOW to communicate, not restrict WHAT."""
        _seed_profile("designer", "non-technical")
        ctx = _builder(tmp_path).build_session_context()
        assert "if they ask for code, provide code" in ctx

    def test_injected_for_custom_agents(self, tmp_path):
        """Profile describes the person, not the workspace — custom agents
        (which skip workspace identity) still get it."""
        _seed_profile("product-manager", "codes")
        ctx = _builder(tmp_path).build_session_context(agent="my-custom-agent")
        assert "[USER PROFILE]" in ctx
        assert "product manager" in ctx

    def test_minimal_context_excludes_profile(self, tmp_path):
        """Minimal-context cron runs stay minimal."""
        _seed_profile("developer", "codes")
        ctx = _builder(tmp_path).build_session_context(minimal_context=True)
        assert "[USER PROFILE]" not in ctx

    def test_ordering_after_agent_identity(self, tmp_path):
        """Block lands with identity context (after CURRENT DATE, before
        workspace identity) so the model reads who it's talking to early."""
        _seed_profile("developer", "codes")
        ctx = _builder(tmp_path).build_session_context(session_key="dashboard:main")
        assert ctx.index("[CURRENT AGENT]") < ctx.index("[USER PROFILE]")
        assert ctx.index("[USER PROFILE]") < ctx.index("[WORKSPACE IDENTITY]")
