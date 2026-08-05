"""Tests for the multi-session evaluation harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp.types import AcpEvent as LLMEvent
from kiro_crew.eval.runner import (
    EvalRunner,
    ScenarioResult,
    SessionResult,
    TurnResult,
    _seed_profile,
    format_results,
    score_by_dimension,
)
from kiro_crew.eval.scenario import (
    Assertion,
    AssertionType,
    Scenario,
    SeedProfile,
    Session,
    Turn,
    load_scenario,
    load_scenarios,
)
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)

# ── Mock Provider ──


class MockProvider:
    """Mock LLM provider that returns scripted responses."""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or [])
        self._call_idx = 0
        self.messages: list[str] = []

    @property
    def cwd(self) -> str:
        """Mirror the LLMProvider.cwd accessor session_map persistence reads."""
        return ""

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def set_workspace(self, path) -> None:
        pass

    async def stream(self, message: str):
        self.messages.append(message)
        if self._call_idx < len(self._responses):
            text = self._responses[self._call_idx]
            self._call_idx += 1
        else:
            text = f"Echo: {message}"
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id):
        pass

    async def reject_tool(self, request_id):
        pass

    def context_usage_pct(self) -> float:
        return 0.0


# ── Assertion Tests ──


class TestAssertion:
    def test_contains(self):
        a = Assertion(type=AssertionType.CONTAINS, value="rust")
        assert a.check("I love Rust programming") is True
        assert a.check("I love Python") is False

    def test_contains_case_sensitive(self):
        a = Assertion(type=AssertionType.CONTAINS, value="Rust", case_sensitive=True)
        assert a.check("I love Rust") is True
        assert a.check("I love rust") is False

    def test_not_contains(self):
        a = Assertion(type=AssertionType.NOT_CONTAINS, value="error")
        assert a.check("All good") is True
        assert a.check("There was an error") is False

    def test_regex(self):
        a = Assertion(type=AssertionType.REGEX, value=r"go|Go|Golang")
        assert a.check("We use Go for this") is True
        assert a.check("We use Python") is False

    def test_equals(self):
        a = Assertion(type=AssertionType.EQUALS, value="yes")
        assert a.check("yes") is True
        assert a.check("  yes  ") is True
        assert a.check("yes indeed") is False


# ── Scenario Loading Tests ──


class TestScenarioLoading:
    def test_load_json(self, tmp_path):
        data = {
            "name": "json_test",
            "sessions": [
                {
                    "name": "s1",
                    "turns": [{"user": "hello", "assertions": [{"type": "contains", "value": "hi"}]}],
                }
            ],
        }
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data))
        scenario = load_scenario(f)
        assert scenario.name == "json_test"
        assert len(scenario.sessions) == 1
        assert scenario.sessions[0].turns[0].assertions[0].type == AssertionType.CONTAINS

    def test_load_json_with_seed(self, tmp_path):
        data = {
            "name": "seeded",
            "seed": {
                "preferences": "- likes dark mode",
                "projects": "Working on Starfish",
                "lessons": ["always use 2-space indent"],
            },
            "sessions": [{"name": "s1", "turns": [{"user": "hi"}]}],
        }
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data))
        scenario = load_scenario(f)
        assert scenario.seed is not None
        assert "dark mode" in scenario.seed.preferences
        assert "Starfish" in scenario.seed.projects
        assert len(scenario.seed.lessons) == 1

    def test_load_json_no_seed(self, tmp_path):
        data = {"name": "no_seed", "sessions": []}
        f = tmp_path / "test.json"
        f.write_text(json.dumps(data))
        scenario = load_scenario(f)
        assert scenario.seed is None

    def test_load_scenarios_dir(self, tmp_path):
        for i in range(3):
            f = tmp_path / f"s{i}.json"
            f.write_text(json.dumps({"name": f"scenario_{i}", "sessions": []}))
        scenarios = load_scenarios(tmp_path)
        assert len(scenarios) == 3

    def test_load_yaml(self, tmp_path):
        yaml_content = (
            "name: yaml_test\n"
            "sessions:\n"
            "  - name: s1\n"
            "    turns:\n"
            "      - user: hello\n"
            "        assertions:\n"
            "          - type: contains\n"
            "            value: hi\n"
        )
        f = tmp_path / "test.yaml"
        f.write_text(yaml_content)
        scenario = load_scenario(f)
        assert scenario.name == "yaml_test"
        assert len(scenario.sessions) == 1
        assert scenario.sessions[0].turns[0].assertions[0].type == AssertionType.CONTAINS


# ── Tool Safety Tests ──


class TestToolSafety:
    """Parametrized tests for _classify_safe_tool."""

    @pytest.mark.parametrize(
        "tool_name,expected",
        [
            ("read_file", "prefix_fs"),
            ("grep_workspace", "prefix_fs"),
            ("glob_files", "prefix_fs"),
            ("search", "unsafe"),  # ambiguous short name
            ("read", "unsafe"),  # ambiguous short name
            ("search_workspace_files", "prefix_fs"),
            ("describe_resource", "prefix_api"),
            ("web_search_query", "prefix_api"),
            ("WorkspaceSearch", "exact"),
            ("write_file", "unsafe"),
            ("execute_bash", "unsafe"),
            ("delete_resource", "unsafe"),
            ("rm", "unsafe"),
            ("git_push", "unsafe"),
        ],
    )
    def test_classify_safe_tool(self, tool_name, expected):
        class FakeEvent:
            title = tool_name

        assert EvalRunner._classify_safe_tool(FakeEvent()) == expected


# ── Path Extraction Tests ──


class TestExtractPathFromInput:
    @pytest.mark.parametrize(
        "tool_input,expected",
        [
            ("", ""),
            ('{"path": "/src/main.py"}', "/src/main.py"),
            ('{"file": "/etc/passwd"}', "/etc/passwd"),
            ('{"target": "/tmp/out"}', "/tmp/out"),
            ('{"unrelated": "value"}', ""),
            ("cat /src/main.py", "/src/main.py"),
            ("https://example.com/path", ""),  # HTTP URLs should not match
            ("no-slash-token", ""),
            ("invalid json {{", ""),  # falls through to token heuristic
        ],
    )
    def test_extract_path(self, tool_input, expected):
        assert EvalRunner._extract_path_from_input(tool_input) == expected


# ── Profile Seeding Tests ──


class TestSeedProfile:
    def test_seed_preferences(self, tmp_path):
        seed = SeedProfile(preferences="# Prefs\n- dark mode\n")
        _seed_profile(tmp_path, seed)
        prefs = (tmp_path / "memory" / "preferences.md").read_text(encoding="utf-8")
        assert "dark mode" in prefs

    def test_seed_projects(self, tmp_path):
        seed = SeedProfile(projects="Working on Starfish cache")
        _seed_profile(tmp_path, seed)
        projects = (tmp_path / "memory" / "projects.md").read_text(encoding="utf-8")
        assert "Starfish" in projects

    def test_seed_lessons(self, tmp_path):
        seed = SeedProfile(lessons=["use 2-space indent", "prefer pytest"])
        _seed_profile(tmp_path, seed)
        lessons_file = tmp_path / "lessons.jsonl"
        assert lessons_file.exists()
        lines = lessons_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["rule"] == "use 2-space indent"

    def test_seed_empty(self, tmp_path):
        seed = SeedProfile()
        _seed_profile(tmp_path, seed)
        # Should still init memory dir without errors
        assert (tmp_path / "memory").is_dir()


# ── Runner Tests ──
# Note: These tests work because MockProvider is passed via provider_factory and
# EvalRunner.run_scenario uses a temp workspace_dir, which triggers _run_scenario_in
# with lazy imports of SessionManager/KiroCrewConfig. The tests exercise the full
# memory loop (consolidation, vector store) but MockProvider short-circuits actual
# LLM calls, so no real kiro-cli session is needed.


class TestEvalRunner:
    @pytest.mark.asyncio
    async def test_run_scenario_pass(self):
        scenario = Scenario(
            name="simple_pass",
            sessions=[
                Session(
                    name="s1",
                    turns=[Turn(user="hello", assertions=[
                        Assertion(type=AssertionType.CONTAINS, value="echo"),
                    ])],
                )
            ],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: MockProvider())
        result = await runner.run_scenario(scenario)
        assert result.passed is True
        assert result.total_assertions == 1
        assert result.passed_assertions == 1

    @pytest.mark.asyncio
    async def test_run_scenario_fail(self):
        scenario = Scenario(
            name="simple_fail",
            sessions=[
                Session(
                    name="s1",
                    turns=[Turn(user="hello", assertions=[
                        Assertion(type=AssertionType.CONTAINS, value="nonexistent"),
                    ])],
                )
            ],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: MockProvider())
        result = await runner.run_scenario(scenario)
        assert result.passed is False
        assert result.passed_assertions == 0

    @pytest.mark.asyncio
    async def test_multi_session_with_scripted_responses(self):
        """Simulate cross-session memory recall with scripted responses."""
        scenario = Scenario(
            name="memory_test",
            dimensions=["memory_recall"],
            sessions=[
                Session(
                    name="teach",
                    turns=[Turn(
                        user="My favorite language is Rust",
                        assertions=[Assertion(type=AssertionType.CONTAINS, value="rust")],
                    )],
                ),
                Session(
                    name="recall",
                    turns=[Turn(
                        user="What is my favorite language?",
                        assertions=[Assertion(type=AssertionType.CONTAINS, value="rust")],
                    )],
                ),
            ],
        )

        providers_created = []

        def factory(key, **kw):
            if "teach" in key:
                p = MockProvider(["Got it, I'll remember Rust is your favorite!"])
            else:
                p = MockProvider(["Your favorite language is Rust."])
            providers_created.append(p)
            return p

        runner = EvalRunner(provider_factory=factory)
        result = await runner.run_scenario(scenario)
        assert result.passed is True
        assert len(result.sessions) == 2
        assert len(providers_created) >= 2

    @pytest.mark.asyncio
    async def test_seeded_scenario(self, tmp_path):
        """Verify profile seeding happens before sessions run."""
        scenario = Scenario(
            name="seeded_test",
            seed=SeedProfile(preferences="# Prefs\n- likes Rust\n"),
            sessions=[
                Session(name="s1", turns=[Turn(user="hi")]),
            ],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: MockProvider(), workspace_dir=tmp_path)
        await runner.run_scenario(scenario)
        prefs = (tmp_path / "memory" / "preferences.md").read_text(encoding="utf-8")
        assert "Rust" in prefs

    @pytest.mark.asyncio
    async def test_tool_calls_captured(self):
        class ToolProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(kind=EVENT_TOOL_CALL, text="read_file")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done reading")
                yield LLMEvent(kind=EVENT_COMPLETE)

        scenario = Scenario(
            name="tool_test",
            sessions=[Session(name="s1", turns=[Turn(user="read my config")])],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: ToolProvider())
        result = await runner.run_scenario(scenario)
        assert result.sessions[0].turns[0].tool_calls == ["read_file"]


# ── Permission Flow Tests ──


class TestPermissionFlow:
    """Integration tests for the tool permission request flow in _run_turn."""

    @pytest.mark.asyncio
    async def test_exact_match_approved_without_path_check(self):
        """Exact-allowlist tools (e.g. WorkspaceSearch) approved even with non-path input."""
        approved = []
        rejected = []

        class PermProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title="WorkspaceSearch",
                    tool_input='{"searchQuery": "hello"}',
                    request_id="r1",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE)

            async def approve_tool(self, request_id):
                approved.append(request_id)

            async def reject_tool(self, request_id):
                rejected.append(request_id)

        scenario = Scenario(
            name="perm_exact",
            sessions=[Session(name="s1", turns=[Turn(user="search")])],
        )
        runner = EvalRunner(provider_factory=lambda key, **kw: PermProvider())
        await runner.run_scenario(scenario)
        assert approved == ["r1"]
        assert rejected == []

    @pytest.mark.asyncio
    async def test_prefix_match_no_path_rejected(self):
        """Prefix-match tool with non-empty input but no extractable path is rejected."""
        approved = []
        rejected = []

        class PermProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title="read_file",
                    tool_input="some-opaque-input-no-path",
                    request_id="r1",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE)

            async def approve_tool(self, request_id):
                approved.append(request_id)

            async def reject_tool(self, request_id):
                rejected.append(request_id)

        scenario = Scenario(
            name="perm_nopath",
            sessions=[Session(name="s1", turns=[Turn(user="read")])],
        )
        runner = EvalRunner(provider_factory=lambda key, **kw: PermProvider())
        await runner.run_scenario(scenario)
        assert approved == []
        assert rejected == ["r1"]

    @pytest.mark.asyncio
    async def test_unsafe_tool_rejected(self):
        """Unsafe tools are rejected regardless of tool_input."""
        approved = []
        rejected = []

        class PermProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title="write_file",
                    tool_input='{"path": "/tmp/safe.txt"}',
                    request_id="r1",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE)

            async def approve_tool(self, request_id):
                approved.append(request_id)

            async def reject_tool(self, request_id):
                rejected.append(request_id)

        scenario = Scenario(
            name="perm_unsafe",
            sessions=[Session(name="s1", turns=[Turn(user="write")])],
        )
        runner = EvalRunner(provider_factory=lambda key, **kw: PermProvider())
        await runner.run_scenario(scenario)
        assert approved == []
        assert rejected == ["r1"]

    @pytest.mark.asyncio
    async def test_prefix_fs_valid_path_approved(self):
        """Prefix-match FS tool with a valid non-sensitive path is approved."""
        approved = []
        rejected = []

        class PermProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title="read_file",
                    tool_input='{"path": "/tmp/safe.txt"}',
                    request_id="r1",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE)

            async def approve_tool(self, request_id):
                approved.append(request_id)

            async def reject_tool(self, request_id):
                rejected.append(request_id)

        scenario = Scenario(
            name="perm_fs_valid",
            sessions=[Session(name="s1", turns=[Turn(user="read")])],
        )
        runner = EvalRunner(provider_factory=lambda key, **kw: PermProvider())
        await runner.run_scenario(scenario)
        assert approved == ["r1"]
        assert rejected == []

    @pytest.mark.asyncio
    async def test_prefix_fs_sensitive_path_rejected(self):
        """Prefix-match FS tool targeting a sensitive path is rejected."""
        approved = []
        rejected = []
        sensitive_path = str(Path.home() / ".aws" / "credentials")

        class PermProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title="read_file",
                    tool_input=json.dumps({"path": sensitive_path}),
                    request_id="r1",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
                yield LLMEvent(kind=EVENT_COMPLETE)

            async def approve_tool(self, request_id):
                approved.append(request_id)

            async def reject_tool(self, request_id):
                rejected.append(request_id)

        scenario = Scenario(
            name="perm_fs_sensitive",
            sessions=[Session(name="s1", turns=[Turn(user="read")])],
        )
        runner = EvalRunner(provider_factory=lambda key, **kw: PermProvider())
        await runner.run_scenario(scenario)
        assert approved == []
        assert rejected == ["r1"]


# ── Dimension Scoring Tests ──


class TestDimensionScoring:
    def test_score_by_dimension(self):
        results = [
            ScenarioResult(
                name="a",
                dimensions=["memory_recall"],
                sessions=[SessionResult(name="s1", turns=[
                    TurnResult(
                        user_message="q",
                        agent_response="r",
                        assertion_results=[
                            (Assertion(type=AssertionType.CONTAINS, value="x"), True),
                            (Assertion(type=AssertionType.CONTAINS, value="y"), False),
                        ],
                    ),
                ])],
            ),
            ScenarioResult(
                name="b",
                dimensions=["memory_recall", "lesson_application"],
                sessions=[SessionResult(name="s1", turns=[
                    TurnResult(
                        user_message="q",
                        agent_response="r",
                        assertion_results=[
                            (Assertion(type=AssertionType.CONTAINS, value="z"), True),
                        ],
                    ),
                ])],
            ),
        ]
        dims = score_by_dimension(results)
        # Scenario-level scoring: "a" failed (1 assertion failed), "b" passed
        assert dims["memory_recall"]["total"] == 2
        assert dims["memory_recall"]["passed"] == 1
        assert dims["lesson_application"]["total"] == 1
        assert dims["lesson_application"]["passed"] == 1
        assert dims["lesson_application"]["rate"] == 1.0

    def test_score_empty(self):
        dims = score_by_dimension([])
        assert dims == {}


# ── Reporting Tests ──


class TestReporting:
    def test_format_results(self):
        result = ScenarioResult(name="test", description="A test scenario")
        report = format_results([result])
        assert "# Eval Results" in report
        assert "test" in report

    def test_format_results_with_dimensions(self):
        result = ScenarioResult(
            name="test",
            dimensions=["memory_recall"],
            sessions=[SessionResult(name="s1", turns=[
                TurnResult(
                    user_message="q",
                    agent_response="r",
                    assertion_results=[
                        (Assertion(type=AssertionType.CONTAINS, value="r"), True),
                    ],
                ),
            ])],
        )
        report = format_results([result])
        assert "Scorecard by Dimension" in report
        assert "memory_recall" in report

    def test_summary(self):
        result = ScenarioResult(
            name="test",
            dimensions=["memory_recall"],
            elapsed_secs=1.5,
        )
        s = result.summary()
        assert s["name"] == "test"
        assert s["passed"] is True
        assert s["assertions"] == "0/0"
        assert s["elapsed_secs"] == 1.5


# ── Judge Tests ──


class TestJudgeParsing:
    """Tests for LLMJudge JSON parsing."""

    @pytest.mark.asyncio
    async def test_judge_parses_valid_json(self):
        from kiro_crew.eval.judge import LLMJudge

        class JudgeProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text='{"score": 4, "reason": "mostly correct"}',
                )
                yield LLMEvent(kind=EVENT_COMPLETE)

        judge = LLMJudge(provider_factory=lambda key, **kw: JudgeProvider())
        await judge.start()
        verdict = await judge.judge_turn("desc", "criteria", "user msg", "assistant msg")
        await judge.shutdown()
        assert verdict.score == 4.0
        assert verdict.reason == "mostly correct"

    @pytest.mark.asyncio
    async def test_judge_handles_unparseable_response(self):
        from kiro_crew.eval.judge import LLMJudge

        class BadProvider(MockProvider):
            async def stream(self, message):
                self.messages.append(message)
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="not json at all")
                yield LLMEvent(kind=EVENT_COMPLETE)

        judge = LLMJudge(provider_factory=lambda key, **kw: BadProvider())
        await judge.start()
        verdict = await judge.judge_turn("desc", "criteria", "user msg", "assistant msg")
        await judge.shutdown()
        assert verdict.score == 0
        assert "parse_error" in verdict.reason


class TestJudgeAssertionType:
    def test_judge_assertion_always_passes(self):
        a = Assertion(type=AssertionType.JUDGE, value="some criteria")
        assert a.check("any response") is True
        assert a.check("") is True


# ── Consolidation Failure Tests ──


class TestConsolidationFailure:
    @pytest.mark.asyncio
    async def test_consolidation_failure_tracked(self):
        """Verify consolidation_failures increments when consolidation raises."""
        from unittest.mock import AsyncMock, patch

        scenario = Scenario(
            name="consol_fail",
            sessions=[
                Session(
                    name="s1",
                    turns=[Turn(user="hello", assertions=[
                        Assertion(type=AssertionType.CONTAINS, value="echo"),
                    ])],
                ),
            ],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: MockProvider())

        with patch(
            "kiro_crew.history.HistoryConsolidator._consolidate",
            new_callable=AsyncMock,
            side_effect=RuntimeError("consolidation boom"),
        ):
            result = await runner.run_scenario(scenario)

        assert result.consolidation_failures > 0
        # Session data should still be present despite consolidation failure
        assert len(result.sessions) == 1
        assert len(result.sessions[0].turns) == 1


# ── JUDGE Filtering Tests ──


class TestJudgeFiltering:
    @pytest.mark.asyncio
    async def test_judge_assertions_excluded_without_flag(self):
        """JUDGE assertions are excluded from results when judge_enabled=False."""
        scenario = Scenario(
            name="judge_filter",
            sessions=[
                Session(
                    name="s1",
                    turns=[Turn(user="hello", assertions=[
                        Assertion(type=AssertionType.CONTAINS, value="echo"),
                        Assertion(type=AssertionType.JUDGE, value="quality check"),
                    ])],
                ),
            ],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: MockProvider(), judge_enabled=False)
        result = await runner.run_scenario(scenario)
        # Only the CONTAINS assertion should be in results
        assert result.total_assertions == 1
        assert result.passed_assertions == 1

    @pytest.mark.asyncio
    async def test_judge_assertions_included_with_flag(self):
        """JUDGE assertions are included when judge_enabled=True."""
        scenario = Scenario(
            name="judge_include",
            sessions=[
                Session(
                    name="s1",
                    turns=[Turn(user="hello", assertions=[
                        Assertion(type=AssertionType.CONTAINS, value="echo"),
                        Assertion(type=AssertionType.JUDGE, value="quality check"),
                    ])],
                ),
            ],
        )

        runner = EvalRunner(provider_factory=lambda key, **kw: MockProvider(), judge_enabled=True)
        result = await runner.run_scenario(scenario)
        # Both assertions should be in results
        assert result.total_assertions == 2
