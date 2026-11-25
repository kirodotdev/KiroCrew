"""Every harness gets a ``[TOOL NAMES]`` block that names its own loader.

Prompts and skills say "load it as the ``[TOOL NAMES]`` block says" instead of
naming a loader, so each provider label must map to the loader that harness
really has, and to nobody else's.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.types import (
    PROVIDER_LABEL_ANTIGRAVITY,
    PROVIDER_LABEL_BY_BACKEND,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_COPILOT,
    PROVIDER_LABEL_DEEPSEEK,
    PROVIDER_LABEL_DEFAULT,
    PROVIDER_LABEL_GOOSE,
    PROVIDER_LABEL_KAS,
    PROVIDER_LABEL_MIMOCODE,
    PROVIDER_LABEL_OPENCODE,
    PROVIDER_LABEL_PI,
    PROVIDER_LABEL_STRANDS,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly.tool_names import tool_names_block
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

#: Each harness's own loader text, and every other harness's, which must be absent.
_LOADERS = {
    "kiro-cli": 'tool_search(tool_id="<server>::<tool>")',
    "kas": "pass its `tool_id` unchanged",
    "claude": '`query: "select:mcp__<server>__<tool>"`',
    "mimocode": "`mcp_tool_search`",
    "direct": "no loading step is needed",
}

_DIRECT = (
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_OPENCODE,
    PROVIDER_LABEL_PI,
    PROVIDER_LABEL_GOOSE,
    PROVIDER_LABEL_DEEPSEEK,
    PROVIDER_LABEL_COPILOT,
    PROVIDER_LABEL_STRANDS,
    PROVIDER_LABEL_ANTIGRAVITY,
)
_EXPECTED = {
    PROVIDER_LABEL_DEFAULT: "kiro-cli",
    PROVIDER_LABEL_KAS: "kas",
    PROVIDER_LABEL_CLAUDE: "claude",
    PROVIDER_LABEL_MIMOCODE: "mimocode",
    **{label: "direct" for label in _DIRECT},
}


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here may build a ``ContextBuilder``: close its ``SkillsLoader``."""


def _builder(tmp_path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def _block_of(context: str) -> str:
    start = context.index("[TOOL NAMES]")
    end = context.index("[End of tool names]", start)
    return context[start:end]


@pytest.mark.parametrize("label", sorted(set(PROVIDER_LABEL_BY_BACKEND.values())))
def test_every_known_harness_gets_a_block(label):
    """A harness must choose its block rather than silently get none."""
    assert tool_names_block(label).startswith("[TOOL NAMES]\n")


@pytest.mark.parametrize("label", sorted(_EXPECTED))
def test_the_block_names_this_harness_loader_and_no_other(label):
    block = tool_names_block(label)
    assert block.startswith("[TOOL NAMES]\n")
    assert block.endswith("[End of tool names]\n\n")
    own = _EXPECTED[label]
    assert _LOADERS[own] in block
    for other, text in _LOADERS.items():
        if other != own:
            assert text not in block, (label, other)


def test_only_kiro_cli_names_tool_search():
    """``tool_search`` exists only on kiro-cli; no other block may send a model there."""
    for label in _EXPECTED:
        block = tool_names_block(label)
        named = "`tool_search`" in block or "tool_search(" in block
        assert named is (label == PROVIDER_LABEL_DEFAULT), label


def test_the_kiro_cli_block_reads_its_refusal_as_deferral():
    """kiro-cli's own refusal text, and the exact-id call that recovers from it."""
    block = tool_names_block(PROVIDER_LABEL_DEFAULT)
    assert "`A tool with the name '<name>' does not exist` means DEFERRED, not missing" in block
    assert "keyword `query` can score below the match threshold" in block


@pytest.mark.parametrize("label", sorted(_EXPECTED))
@pytest.mark.parametrize("minimal", [False, True])
def test_conductor_and_bot_get_the_same_block(tmp_path, label, minimal):
    """Conductor mode, the default Bot and a custom Bot read one block per harness."""
    blocks = {
        agent: _block_of(
            _builder(tmp_path).build_session_context(
                session_key=f"dashboard:chat-parity-{agent}",
                agent=agent,
                provider_type=label,
                minimal_context=minimal,
            )
        )
        for agent in ("kirocrew-conductor", "kirocrew", "my-bot")
    }
    assert len(set(blocks.values())) == 1
    assert blocks["kirocrew"] + "[End of tool names]\n\n" == tool_names_block(label)


def test_prompts_and_skills_point_at_the_block_instead_of_a_loader():
    """Shipped prose names no harness's loader; the block carries it."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    paths = [
        src / "agent_parts" / "spec_prompts.py",
        *sorted((src / "builtin_skills").rglob("*.md")),
    ]
    # An agent spec's ``tools`` grant is configuration, not loader prose: kiro-cli
    # and KAS both read the ``tool_search`` entry to decide deferral.
    grants = {src / "builtin_skills" / "kirocrew-dev" / "dashboard-template" / "agent-spec.md"}
    offenders = [
        str(p.relative_to(src))
        for p in paths
        if p not in grants
        and ("`tool_search`" in (text := p.read_text("utf-8")) or "tool_search(" in text)
    ]
    assert offenders == []
