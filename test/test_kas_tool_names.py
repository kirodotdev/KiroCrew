"""Tool context follows active tool definitions and discovery results.

Workers and conductors receive the same guidance at session start and after
compaction. Bare work-tool names identify what to look up; they do not prescribe
an executor ID. Qualified-name examples remain available for legacy direct calls.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew import mcp_work
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_KAS,
)
from kiro_crew.agent_sdk.kas_tool_names import (
    KAS_MCP_TOOL_ID_MAX_LEN,
    KAS_TOOL_UNAVAILABLE_RE,
    is_kas_provider_label,
    kas_mcp_display_name,
    kas_mcp_tool_id,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import tool_names
from kiro_crew.learn import LessonStore
from kiro_crew.mcp_tools import build_tool_names
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

WORK = "kirocrew-work"
CORE = "kirocrew-core"


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here may build a ``ContextBuilder``: close its ``SkillsLoader``."""


def _builder(tmp_path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def _work_tools() -> list[tuple[str, str]]:
    return [(WORK, tool["name"]) for tool in mcp_work._tool_definitions()]


def _direct_resolve(called: str, tools: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Resolve only the direct-call names described by the public naming helpers."""
    for server, tool in tools:
        if called == kas_mcp_tool_id(server, tool):
            return server, tool
    for server, tool in tools:
        if called == kas_mcp_display_name(server, tool):
            return server, tool
    return None


def _resolvable_names(context: str, tools: list[tuple[str, str]]) -> dict[str, str]:
    """Find qualified direct-call examples that reach the real work-tool definitions."""
    found: dict[str, str] = {}
    for name in re.findall(r"`([^`\s]+)`", context):
        hit = _direct_resolve(name, tools)
        if hit is not None:
            found.setdefault(hit[1], name)
    return found


def _assert_active_tool_guidance(context: str) -> tuple[str, str]:
    """Assert the legacy fallback and the executor path are separate instructions."""
    assert "[TOOL NAMES]" in context
    block = context.split("[TOOL NAMES]", 1)[1].split("[End of tool names]", 1)[0]
    direct, deferred = block.split("Deferred tools and executors:", 1)
    assert "call the exact name in its active tool definition" in direct
    assert "returns only a bare `tool_name` and no executor" in direct
    assert "use the qualified direct-call name, not the bare name" in direct
    assert "retry with the qualified direct-call name" in direct
    assert "never to loader inputs or executor IDs" in direct
    assert "copy the exact ID from the current catalogue" in deferred
    assert "do not construct it from the direct-call examples above" in deferred
    assert "wait for the schema result before calling" in deferred
    assert "use that executor, not a direct MCP call" in deferred
    assert "pass its `tool_id` unchanged" in deferred
    assert "Do not construct or rewrite that ID" in deferred
    assert "schema-load result is still in context" in deferred
    assert "check the current catalogue and the loader's recovery guidance" in deferred
    assert "do not assume a name rewrite will fix it" in deferred
    assert "mcp_<server>_<tool>" not in deferred
    assert "including after tool_search loads it" not in block
    assert "Retry the same call at once" not in block
    return direct, deferred


class TestActiveToolContract:
    def test_loader_ids_are_copied_before_loading_not_built_from_direct_names(self):
        block = tool_names.tool_names_block(PROVIDER_LABEL_KAS)
        assert "copy the exact ID from the current catalogue" in block
        assert "do not construct it from the direct-call examples above" in block

    def test_executor_result_takes_precedence_over_direct_call_fallback(self):
        block = tool_names.tool_names_block(PROVIDER_LABEL_KAS)
        assert (
            "If the result specifies an executor, use that executor, not a direct MCP call" in block
        )
        assert "pass its `tool_id` unchanged" in block
        assert (
            "This fallback applies only to direct calls, never to loader inputs or executor IDs"
            in block
        )

    @pytest.mark.parametrize(
        ("agent", "needed"),
        [
            ("kirocrew-worker", ("work_brief", "work_report")),
            ("kirocrew-conductor", ("work_ledger_record",)),
        ],
    )
    def test_every_work_tool_the_session_needs_has_a_qualified_direct_name(
        self, tmp_path, agent, needed
    ):
        context = _builder(tmp_path).build_session_context(
            session_key="dashboard:chat-kas-1", agent=agent, provider_type=PROVIDER_LABEL_KAS
        )
        direct, deferred = _assert_active_tool_guidance(context)
        resolvable = _resolvable_names(direct, _work_tools())
        assert set(needed) <= set(resolvable)
        assert _resolvable_names(deferred, _work_tools()) == {}

    def test_legacy_bare_names_still_do_not_resolve_as_direct_calls(self):
        """Executor support does not make a bare operation name directly callable."""
        for tool in ("work_brief", "work_report", "work_ledger_record"):
            assert _direct_resolve(tool, _work_tools()) is None

    def test_a_minimal_session_start_carries_the_block_too(self, tmp_path):
        context = _builder(tmp_path).build_session_context(
            session_key="dashboard:chat-kas-2",
            agent="kirocrew-worker",
            provider_type=PROVIDER_LABEL_KAS,
            minimal_context=True,
        )
        direct, deferred = _assert_active_tool_guidance(context)
        assert set(_resolvable_names(direct, _work_tools())) >= {"work_brief", "work_report"}
        assert _resolvable_names(deferred, _work_tools()) == {}

    @pytest.mark.parametrize(
        ("backend", "expected"), [(ACP_BACKEND_KAS, True), (ACP_BACKEND_KIRO, False)]
    )
    def test_a_turn_reads_the_harness_off_its_provider_not_the_config(
        self, tmp_path, backend, expected
    ):
        """The dashboard, channels, crons and subagents pass ``agent.provider``
        (``acp``) as ``provider_type`` and the live provider as ``context_provider``;
        the block must follow the provider."""
        from types import SimpleNamespace

        from kiro_crew.providers.acp import AcpProvider

        class _Provider(AcpProvider):
            def __init__(self) -> None:
                self._client = SimpleNamespace(backend=backend, cwd=str(tmp_path))

        message, _ = _builder(tmp_path).build_message(
            "go",
            True,
            session_key="dashboard:chat-kas-4",
            agent="kirocrew-worker",
            provider_type=PROVIDER_LABEL_DEFAULT,
            context_provider=_Provider(),
        )
        assert ("[TOOL NAMES]" in message) is expected

    def test_the_post_compaction_reinjection_carries_the_block(self, tmp_path):
        from kiro_crew.context_assembly import turn as _turn

        parts = _turn.post_compaction_parts(
            _builder(tmp_path),
            session_key="dashboard:chat-kas-3",
            agent="kirocrew-worker",
            project=None,
            mode="",
            is_cc=False,
            private_owner=False,
            blocks_reads=False,
            minimal_context=True,
            context_groups=None,
            workspace=None,
            memory_store=None,
            model_window=None,
            runtime_source=None,
            steering_dirs=(),
            essentials="",
            provider_type=PROVIDER_LABEL_KAS,
            context_provider=None,
            native_launch_sources=(),
        )
        direct, deferred = _assert_active_tool_guidance("".join(parts))
        assert set(_resolvable_names(direct, _work_tools())) >= {
            "work_brief",
            "work_report",
            "work_ledger_record",
        }
        assert _resolvable_names(deferred, _work_tools()) == {}


class TestOtherHarnessesAreUnchanged:
    @pytest.mark.parametrize(
        "provider_type",
        [PROVIDER_LABEL_DEFAULT, None, PROVIDER_LABEL_CLAUDE, PROVIDER_LABEL_CODEX],
    )
    def test_no_block_off_kas(self, tmp_path, provider_type):
        assert tool_names.tool_names_block(provider_type) == ""
        context = _builder(tmp_path).build_session_context(
            session_key="dashboard:chat-v2", agent="kirocrew-worker", provider_type=provider_type
        )
        assert "[TOOL NAMES]" not in context

    def test_the_label_check_is_positive(self):
        assert is_kas_provider_label(PROVIDER_LABEL_KAS)
        assert not is_kas_provider_label(PROVIDER_LABEL_DEFAULT)
        assert not is_kas_provider_label("")


class TestKasNamingRule:
    """``kas_mcp_tool_id`` against the bundle's ``Vnc``: fuse, ``[\\s-]`` -> ``_``,
    drop the rest of ``[^a-zA-Z0-9_]``, lower-case, cut to 64."""

    @pytest.mark.parametrize(
        ("server", "tool", "expected"),
        [
            ("kirocrew-work", "work_report", "mcp_kirocrew_work_work_report"),
            ("kirocrew-core", "memory_recall", "mcp_kirocrew_core_memory_recall"),
            ("My Server", "Do.Thing", "mcp_my_server_dothing"),
            ("docs.server", "x-y", "mcp_docsserver_x_y"),
        ],
    )
    def test_ids(self, server, tool, expected):
        assert kas_mcp_tool_id(server, tool) == expected

    def test_long_names_are_cut_to_the_cap(self):
        assert len(kas_mcp_tool_id("s", "t" * 200)) == KAS_MCP_TOOL_ID_MAX_LEN

    def test_display_name(self):
        assert kas_mcp_display_name("kirocrew-work", "work_report") == "@kirocrew-work/work_report"

    def test_every_crew_tool_id_is_under_the_cap(self):
        """Direct Crew tool names fit without truncation in the native-name helper."""
        pairs = [(CORE, name) for name in build_tool_names()] + _work_tools()
        too_long = [p for p in pairs if len(kas_mcp_tool_id(*p)) >= KAS_MCP_TOOL_ID_MAX_LEN]
        assert too_long == []

    def test_the_block_examples_follow_the_direct_call_rule_only(self):
        direct, deferred = _assert_active_tool_guidance(
            tool_names.tool_names_block(PROVIDER_LABEL_KAS)
        )
        for server, tool in tool_names._EXAMPLES:
            assert f"`{tool}` on `{server}` is `{kas_mcp_tool_id(server, tool)}`" in direct
            assert f"`{kas_mcp_tool_id(server, tool)}`" not in deferred
            assert f"`{kas_mcp_display_name(server, tool)}`" not in deferred

    def test_the_unavailable_answer_names_the_called_tool(self):
        match = KAS_TOOL_UNAVAILABLE_RE.search('Tool "work_report" is not available.')
        assert match is not None and match.group("name") == "work_report"
