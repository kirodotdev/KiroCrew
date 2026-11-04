"""Tool context follows active tool definitions and discovery results.

Workers and conductors receive the same guidance at session start and after
compaction. Bare work-tool names identify what to look up; they do not prescribe
an execution ID. The native-name helpers remain covered for directly exposed tools.
"""

from __future__ import annotations

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


def _kas_resolve(called: str, tools: list[tuple[str, str]]) -> tuple[str, str] | None:
    """KAS's lookup: ``r.find(t => t.id === name) ?? r.find(t => t.displayName === name)``."""
    for server, tool in tools:
        if called == kas_mcp_tool_id(server, tool):
            return server, tool
    for server, tool in tools:
        if called == kas_mcp_display_name(server, tool):
            return server, tool
    return None


def _assert_active_tool_guidance(context: str) -> str:
    """Check the model-facing block rather than unrelated names in the agent prompt."""
    assert "[TOOL NAMES]" in context
    block = context.split("[TOOL NAMES]", 1)[1].split("[End of tool names]", 1)[0]
    assert "call the exact name in its active tool definition" in block
    assert "wait for the schema result before calling" in block
    assert "pass its `tool_id` unchanged" in block
    assert "Do not construct or rewrite that ID" in block
    assert "schema-load result is still in context" in block
    assert "check the current catalogue and the loader's recovery guidance" in block
    assert "do not assume a name rewrite will fix it" in block
    assert "mcp_<server>_<tool>" not in block
    assert "including after tool_search loads it" not in block
    assert "Retry the same call at once" not in block
    return block


class TestActiveToolContract:
    @pytest.mark.parametrize(
        ("agent", "needed"),
        [
            ("kirocrew-worker", ("work_brief", "work_report")),
            ("kirocrew-conductor", ("work_ledger_record",)),
        ],
    )
    def test_needed_work_tools_are_lookup_hints_under_the_active_contract(
        self, tmp_path, agent, needed
    ):
        context = _builder(tmp_path).build_session_context(
            session_key="dashboard:chat-kas-1", agent=agent, provider_type=PROVIDER_LABEL_KAS
        )
        block = _assert_active_tool_guidance(context)
        for tool in needed:
            assert (WORK, tool) in _work_tools()
            assert f"`{tool}` on `{WORK}`" in block
            assert f"`{kas_mcp_tool_id(WORK, tool)}`" not in block
            assert f"`{kas_mcp_display_name(WORK, tool)}`" not in block

    def test_the_bare_name_the_prompts_use_is_not_one_kas_resolves(self):
        """Why the block exists: the spelling Crew's prompts use reaches nothing on KAS."""
        for tool in ("work_brief", "work_report", "work_ledger_record"):
            assert _kas_resolve(tool, _work_tools()) is None

    def test_a_minimal_session_start_carries_the_block_too(self, tmp_path):
        context = _builder(tmp_path).build_session_context(
            session_key="dashboard:chat-kas-2",
            agent="kirocrew-worker",
            provider_type=PROVIDER_LABEL_KAS,
            minimal_context=True,
        )
        block = _assert_active_tool_guidance(context)
        for tool in ("work_brief", "work_report"):
            assert f"`{tool}` on `{WORK}`" in block

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
        block = _assert_active_tool_guidance("".join(parts))
        for tool in ("work_brief", "work_report", "work_ledger_record"):
            assert f"`{tool}` on `{WORK}`" in block


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

    def test_the_block_examples_are_lookup_pairs_not_execution_ids(self):
        block = _assert_active_tool_guidance(tool_names.tool_names_block(PROVIDER_LABEL_KAS))
        for server, tool in tool_names._EXAMPLES:
            assert f"`{tool}` on `{server}`" in block
            assert f"`{kas_mcp_tool_id(server, tool)}`" not in block
            assert f"`{kas_mcp_display_name(server, tool)}`" not in block

    def test_the_unavailable_answer_names_the_called_tool(self):
        match = KAS_TOOL_UNAVAILABLE_RE.search('Tool "work_report" is not available.')
        assert match is not None and match.group("name") == "work_report"
