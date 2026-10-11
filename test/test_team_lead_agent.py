# Copyright Kiro Crew contributors
# SPDX-License-Identifier: Apache-2.0
"""``kirocrew-team-lead`` ships as a default agent, built like ``kirocrew-research``.

Two things are worth a test and nothing else is. The installer is fifteen lines over
``build_agent_config()``, so what can go wrong is the identity it swaps in -- and that
it adds a surface of its own, which is the thing this design exists not to do. The
skill carries five rules whose whole value is that each one names a MECHANISM rather
than asking for judgement, so each is pinned by that mechanism's name.

There is deliberately no test here for hand-edit detection, ownership digests, a start
gate or a payload generation. The spec is overwritten on every boot exactly as
``kirocrew-research`` is, so "is this file ours?" is never asked and there is nothing
to pin.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from kiro_crew import agent
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES, TEAM_LEAD_AGENT_FILENAME
from kiro_crew.agent_materialization import service_agents

_SKILL = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "team-lead"
    / "SKILL.md"
)


@pytest.fixture
def installed(tmp_path, monkeypatch) -> dict:
    """The spec this installer actually writes, read back off disk."""
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    service_agents._install_team_lead_agent()
    return json.loads((tmp_path / TEAM_LEAD_AGENT_FILENAME).read_text(encoding="utf-8"))


class TestTheInstallerWritesADefaultAgent:
    """Mirrors what ``_install_research_agent`` is held to, on the same three fields."""

    def test_the_file_carries_this_agent_name_and_charter(self, installed):
        assert installed["name"] == "kirocrew-team-lead"
        assert installed["prompt"] == agent._TEAM_LEAD_SYSTEM_PROMPT
        assert installed["prompt"].startswith("# Kiro Crew Team Lead")
        assert installed["description"]

    def test_the_filename_is_owned_so_the_boot_sweep_covers_it(self):
        assert TEAM_LEAD_AGENT_FILENAME == "kirocrew-team-lead.json"
        assert TEAM_LEAD_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_it_adds_no_surface_of_its_own(self, installed, tmp_path, monkeypatch):
        """THE property the whole design rests on. The spec is whatever
        ``build_agent_config`` already filtered against the governance ceiling, with the
        guide server dropped and three identity fields swapped -- so a ceiling change
        re-filters one surface rather than two, and there is no grant here for a review
        to judge separately. An installer that appended a tool, a server or an
        ``autoApprove`` entry would make this agent's surface its own problem."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        baseline = service_agents._without_guide_server(agent.build_agent_config())
        identity = {"name", "description", "prompt"}
        assert set(installed) == set(baseline), "the installer added or dropped a key"
        for key in set(installed) - identity:
            assert installed[key] == baseline[key], f"{key} was changed by this installer"
        assert "autoApprove" not in json.dumps(installed)

    def test_it_overwrites_whatever_is_there(self, tmp_path, monkeypatch):
        """The contract an operator is told in one line: a customized copy belongs under
        another name, because this one is rewritten every boot. Asserted rather than
        documented, since it is the reason no ownership apparatus is needed."""
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        target = tmp_path / TEAM_LEAD_AGENT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"name": "mine", "allowedTools": []}), encoding="utf-8")

        service_agents._install_team_lead_agent()

        assert json.loads(target.read_text(encoding="utf-8"))["name"] == "kirocrew-team-lead"


class TestTheSkillCarriesTheTeamManagementRules:
    """Five calls about the TEAM, each pinned to the mechanism that decides it.

    Pinned on the mechanism NAME rather than on wording, so the text can be copy-edited
    without rewriting a test, and partitioned to the section that owns the rules so a
    phrase sitting elsewhere in the file cannot satisfy a pin.
    """

    @pytest.fixture
    def section(self) -> str:
        text = _SKILL.read_text(encoding="utf-8")
        head = "## 5. Running the team itself"
        assert head in text, "the team-management section is gone"
        return text.split(head, 1)[1].split("\n## ", 1)[0]

    def test_the_section_was_read_with_content(self, section):
        """THE positive control, and it runs first. Every assertion below is an ``in``
        against one string, which an empty read would satisfy for the absence half, so
        this locks in that the section arrived and that a phrase genuinely absent from
        it is reported absent."""
        assert len(section) > 1000
        assert "a phrase no rule in this section contains" not in section

    def test_capacity_decides_the_wave(self, section):
        assert "resource_status" in section
        for posture in ("ample", "tight", "critical", "unknown"):
            assert f"`{posture}`" in section, f"the posture list is missing {posture}"
        assert "MAX_SLOTS_PER_CREATOR" in section and "MAX_LIVE_SLOTS" in section
        assert "reseed wave" in section

    def test_the_depth_cap_is_what_refuses_a_third_level(self, section):
        assert "MAX_DEPTH" in section and "work_ledger.py" in section
        assert "18127" in section, "the tracked guard mismatch is not named"

    def test_merging_two_lines_closes_and_reseeds(self, section):
        assert "action=close" in section
        assert "session_adopt" in section, "the verb this is NOT is not named"

    def test_a_tracker_is_dispatched_when_one_read_stops_fitting(self, section):
        assert "`compact` ledger read comes back cut" in section
        assert "reports and never" in section

    def test_one_owner_per_shared_file_and_one_integrator_per_output(self, section):
        assert "one integrator per output" in section
        assert "different child than the" in section, "the separate-reviewer clause is gone"

    def test_the_skill_describes_a_default_agent_not_a_crewmate(self):
        """The ruling in one assertion. The dashboard verbs answer only inside a
        crewmate's own thread, so a skill that still instructs the lead to write a board
        would be telling it to call something that refuses."""
        text = _SKILL.read_text(encoding="utf-8").lower()
        for gone in ("crewmate", "dashboard_write", "dashboard_fields"):
            assert gone not in text, f"{gone!r} survived in the skill"
