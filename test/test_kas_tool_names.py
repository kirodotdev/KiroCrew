"""On KAS, a dispatched worker and its conductor are told a work-tool name KAS resolves.

KAS answers a model's tool call by the tool's id (``mcp_<server>_<tool>``) or its
display name (``@<server>/<tool>``) and by nothing else; a bare ``work_report`` comes
back as ``Tool "work_report" is not available.`` without reaching the server. Kiro
Crew's prompts name the work tools bare, so these tests build the session context a
KAS worker and a KAS conductor actually receive, collect every tool name it spells,
and run each through a mirror of KAS's resolver over the work server's real tools.
Without the ``[TOOL NAMES]`` block the context names the tools only bare and nothing
resolves.
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


def _kas_resolve(called: str, tools: list[tuple[str, str]]) -> tuple[str, str] | None:
    """KAS's lookup: ``r.find(t => t.id === name) ?? r.find(t => t.displayName === name)``."""
    for server, tool in tools:
        if called == kas_mcp_tool_id(server, tool):
            return server, tool
    for server, tool in tools:
        if called == kas_mcp_display_name(server, tool):
            return server, tool
    return None


def _resolvable_names(context: str, tools: list[tuple[str, str]]) -> dict[str, str]:
    """Every backticked name in *context* KAS would resolve, keyed by the tool it reaches."""
    found: dict[str, str] = {}
    for name in re.findall(r"`([^`\s]+)`", context):
        hit = _kas_resolve(name, tools)
        if hit is not None:
            found.setdefault(hit[1], name)
    return found


class TestKasResolvesWhatTheContextTellsTheModel:
    @pytest.mark.parametrize(
        ("agent", "needed"),
        [
            ("kirocrew-worker", ("work_brief", "work_report")),
            ("kirocrew-conductor", ("work_ledger_record",)),
        ],
    )
    def test_every_work_tool_the_session_needs_has_a_name_kas_resolves(
        self, tmp_path, agent, needed
    ):
        context = _builder(tmp_path).build_session_context(
            session_key="dashboard:chat-kas-1", agent=agent, provider_type=PROVIDER_LABEL_KAS
        )
        resolvable = _resolvable_names(context, _work_tools())
        missing = [tool for tool in needed if tool not in resolvable]
        assert missing == [], f"no KAS-resolvable name for {missing} in the {agent} context"

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
        assert set(_resolvable_names(context, _work_tools())) >= {"work_brief", "work_report"}

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
        assert set(_resolvable_names("".join(parts), _work_tools())) >= {
            "work_brief",
            "work_report",
            "work_ledger_record",
        }


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
        """The block states the rule without the 64-char cut; that is exact only while
        no Crew tool's id reaches the cap."""
        pairs = [(CORE, name) for name in build_tool_names()] + _work_tools()
        too_long = [p for p in pairs if len(kas_mcp_tool_id(*p)) >= KAS_MCP_TOOL_ID_MAX_LEN]
        assert too_long == []

    def test_the_block_examples_follow_the_rule(self):
        block = tool_names.tool_names_block(PROVIDER_LABEL_KAS)
        for server, tool in tool_names._EXAMPLES:
            assert f"`{kas_mcp_tool_id(server, tool)}`" in block

    def test_the_unavailable_answer_names_the_called_tool(self):
        match = KAS_TOOL_UNAVAILABLE_RE.search('Tool "work_report" is not available.')
        assert match is not None and match.group("name") == "work_report"
