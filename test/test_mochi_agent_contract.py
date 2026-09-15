"""The agent-facing contract: what the prompts/skills say must be callable.

These pin the class of bug this file exists for: a prompt that names a tool, a
parameter, or a file that the MCP layer does not actually accept. Every one of
those failures is SILENT — an unknown ``get_watchlist`` argument is ignored, an
unsupported ``update_plan`` key is dropped, and both return ``ok: true`` — so no
amount of unit testing of the implementation alone would surface them. The only
way to catch the drift is to read the shipped instructions and check them against
the schema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.mochi import mcp_server, soul_loader
from kiro_crew.apps.builtins.mochi import watchlist_file as wf

AGENTS = Path(soul_loader.__file__).parent / "agents"
SKILLS = AGENTS / "skills"
CONTEXT = AGENTS / "context"

#: Everything shipped to the agent as instructions.
INSTRUCTION_FILES = sorted(SKILLS.glob("*/SKILL.md")) + sorted(CONTEXT.glob("*.md"))


def _tool(name: str) -> dict:
    for spec in mcp_server._list_tools():
        if spec["name"] == name:
            return spec
    raise AssertionError(f"no such tool: {name}")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestNoInstructionNamesAMissingTool:
    def test_instruction_files_exist(self):
        """Guard the guard: a rename must not silently empty this suite."""
        assert len(INSTRUCTION_FILES) >= 8

    @pytest.mark.parametrize("path", INSTRUCTION_FILES, ids=lambda p: p.parent.name)
    def test_no_skill_loading_tool_is_named(self, path):
        """``SkillsTool`` does not exist here.

        Session context injects the skill catalogue only for the built-in agent,
        and these agents' JSON declares no ``skill://`` resources — so there is no
        tool that resolves a skill by name. The agent reads the file instead, and
        the absolute path is rendered into its prompt.
        """
        assert "SkillsTool" not in _text(path)

    @pytest.mark.parametrize("path", INSTRUCTION_FILES, ids=lambda p: p.parent.name)
    def test_no_tool_that_was_never_implemented(self, path):
        body = _text(path)
        for absent in ("get_battery_status", "write_mochi_file"):
            assert absent not in body, f"{absent} has no implementation"

    @pytest.mark.parametrize("path", INSTRUCTION_FILES, ids=lambda p: p.parent.name)
    def test_read_mochi_file_calls_use_the_real_parameter(self, path):
        """``which``, from a fixed enum — not ``filename``."""
        body = _text(path)
        for call in re.findall(r"read_mochi_file\(\{([^}]*)\}\)", body):
            assert "filename" not in call, f"read_mochi_file takes `which`, not `filename`: {call}"
            names = re.findall(r"which:\s*[\"']([^\"']+)[\"']", call)
            for name in names:
                assert name in mcp_server._READABLE, f"unknown file {name!r}"

    @pytest.mark.parametrize("path", INSTRUCTION_FILES, ids=lambda p: p.parent.name)
    def test_get_watchlist_calls_only_pass_declared_params(self, path):
        declared = set(_tool("get_watchlist")["inputSchema"]["properties"])
        body = _text(path)
        for call in re.findall(r"get_watchlist\(\{([^}]*)\}\)", body):
            for key in re.findall(r"([A-Za-z_]+)\s*:", call):
                assert key in declared, f"get_watchlist has no {key!r} param (silently ignored)"


class TestUpdatePlanSchemaMatchesTheImplementation:
    """The schema is what the model sees; apply_update is what runs."""

    def test_every_declared_key_is_honoured(self):
        from kiro_crew.apps.builtins.mochi import queue_file as qf

        declared = set(_tool("update_plan")["inputSchema"]["properties"])
        source = Path(qf.__file__).read_text(encoding="utf-8")
        for key in declared:
            assert f'"{key}"' in source, f"update_plan declares {key!r} but apply_update ignores it"

    def test_a_bare_tasks_key_is_not_advertised(self):
        """It was, and apply_update dropped it — a no-op plan reported as ok."""
        spec = _tool("update_plan")
        assert "tasks" not in spec["inputSchema"]["properties"]
        assert "full_replace" in spec["inputSchema"]["properties"]

    def test_planner_notes_is_typed_as_the_object_it_is(self):
        props = _tool("update_plan")["inputSchema"]["properties"]
        assert props["planner_notes"]["type"] == "object"


class TestUpdateWatchlistDeclaresWhatItAccepts:
    def test_cancel_is_declared(self):
        """Supported by the implementation and used by the prompts, but undeclared."""
        assert "cancel" in _tool("update_watchlist")["inputSchema"]["properties"]

    def test_every_declared_op_is_honoured(self):
        source = Path(wf.__file__).read_text(encoding="utf-8")
        for op in _tool("update_watchlist")["inputSchema"]["properties"]:
            assert f'params.get("{op}")' in source


class TestRescheduleIsWritable:
    def test_trigger_at_can_be_updated(self):
        """A rescheduled meeting must not keep firing at the old time."""
        item = wf.create_watch_item(
            {"kind": "meeting", "label": "sync", "triggerAt": "2026-01-01T10:00:00Z"},
            now_ms=0,
        )
        out = wf.apply_watchlist_update(
            {"items": [item]},
            {"update": [{"id": item["id"], "triggerAt": "2026-01-01T15:30:00Z"}]},
            now_ms=0,
        )
        updated = out["items"][0]
        assert updated["triggerAt"] == "2026-01-01T15:30:00Z"
        # nextCheckAfter is derived from triggerAt for these kinds; leaving it
        # behind would make the item permanently overdue against the new time.
        assert updated["nextCheckAfter"] == "2026-01-01T15:30:00Z"

    def test_a_watch_items_next_check_is_not_hijacked_by_a_trigger_at(self):
        item = wf.create_watch_item({"kind": "url", "target": "https://x"}, now_ms=0)
        before = item["nextCheckAfter"]
        out = wf.apply_watchlist_update(
            {"items": [item]},
            {"update": [{"id": item["id"], "triggerAt": "2026-01-01T15:30:00Z"}]},
            now_ms=0,
        )
        assert out["items"][0]["nextCheckAfter"] == before


class TestTheUsersCadenceSurvivesBackoff:
    def test_the_declared_interval_is_remembered(self):
        """Adaptive backoff needs a floor, or "daily" collapses on first change."""
        item = wf.create_watch_item(
            {"kind": "url", "target": "https://x", "checkIntervalMins": 1440}, now_ms=0
        )
        assert item["baseIntervalMins"] == 1440

    def test_the_watch_skill_resets_to_the_base_not_a_constant(self):
        body = _text(SKILLS / "mochi-watch" / "SKILL.md")
        assert "baseIntervalMins" in body
        assert "reset interval back to 5" not in body


class TestTheTwoAgentsGetTheirOwnDocument:
    def test_the_background_document_is_wired(self):
        """It shipped unreferenced: both agents were handed the chat prompt."""
        assert soul_loader.BG_BEHAVIOUR_PROMPT.is_file()
        out = soul_loader.render_agent_prompt(
            "Kiro", "persona", behaviour_path=soul_loader.BG_BEHAVIOUR_PROMPT
        )
        assert "SUBAGENT" in out or "subagent" in out

    def test_write_agent_prompts_writes_both(self, tmp_path):
        paths = soul_loader.write_agent_prompts(tmp_path, "Kiro", "persona")
        assert set(paths) == {"mochi", "mochi-bg"}
        assert paths["mochi"] != paths["mochi-bg"]
        # encoding= is required: the prompts carry non-ASCII, and Windows
        # defaults read_text() to cp1252, which cannot decode them.
        assert paths["mochi"].read_text(encoding="utf-8") != paths["mochi-bg"].read_text(
            encoding="utf-8"
        )


class TestSkillsAreReachable:
    def test_every_shipped_skill_is_in_the_catalogue(self):
        on_disk = {p.parent.name for p in SKILLS.glob("*/SKILL.md")}
        assert on_disk == set(soul_loader.SKILL_NAMES)

    def test_every_catalogued_skill_is_declared_in_the_manifest(self):
        """A skill absent from app.json is never linked, so never readable."""
        manifest = json.loads((AGENTS.parent / "app.json").read_text(encoding="utf-8"))
        declared = {Path(s).name for s in manifest["skills"]}
        assert declared == set(soul_loader.SKILL_NAMES)

    def test_the_catalogue_carries_absolute_paths(self):
        block = soul_loader.render_skill_catalogue()
        for name in soul_loader.SKILL_NAMES:
            assert name in block
        # is_absolute(), not startswith("/") — a Windows absolute path is C:\\...
        assert soul_loader.skill_path("mochi-watch").is_absolute()

    def test_a_spawn_line_names_the_file_not_a_tool(self):
        line = soul_loader.load_skill_line("mochi-watch")
        assert "SkillsTool" not in line
        assert line.rstrip().endswith("SKILL.md")


class TestSpawnPromptsDoNotOverclaim:
    def test_the_replan_spawn_loads_the_replan_skill(self):
        """It loaded mochi-plan, which has no replan section — full price, wrong map."""
        from kiro_crew.apps.builtins.mochi import hooks

        prompt = hooks.MochiRuntime.replan_prompt(object())
        assert str(soul_loader.skill_path("mochi-replan")) in prompt
        assert str(soul_loader.skill_path("mochi-plan")) not in prompt

    def test_the_background_preamble_does_not_promise_slack(self):
        """Naming a Slack tool while telling the agent not to doubt its tools is
        an instruction to hallucinate: Slack is only present if the user granted it."""
        from kiro_crew.apps.builtins.mochi.queue_poller import _BG_PREAMBLE

        assert "get_messages" not in _BG_PREAMBLE
