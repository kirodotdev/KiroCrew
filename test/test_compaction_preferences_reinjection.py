"""A compaction drops session start; the owner's preferences come back with it.

The first turn after a compaction re-sends the whole preferences document and the
``pref.*`` rows under the session-start allowance, ranked on that turn's text, from
the store the session started with. A slim resume re-sends them too.
"""

from __future__ import annotations

from pathlib import Path

import member_essential_context_helpers
import pytest

from kiro_crew.context import _PREFS_STARTUP_CAP, ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore

# The rendered turn spells the em dash as "--", so match the label after it.
HEADER = "owner preferences]\n"
DOC_RULE = "Never publish a CR revision directly; upload it as a draft."
RELEVANT = "pref.code_review_draft_only"
QUERY = "upload the zircon review revision"

# The shared member fixture, bound here by name so pytest registers it for this module.
env = member_essential_context_helpers.env


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test builds a ``ContextBuilder``: close its ``SkillsLoader``."""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    return root


def _store(tmp_path: Path, opened, name: str, doc: str) -> MemoryStore:
    vectors = opened(VectorMemoryStore(db_path=tmp_path / f"{name}.db"))
    vectors.init()
    # Sixty filler rows put the pref.* block well over its startup allowance.
    for i in range(60):
        vectors.set_semantic(f"pref.rule_{i:02d}", f"standing rule {i} " * 25, 0.9, "user_explicit")
    vectors.set_semantic(
        RELEVANT, "Upload a zircon review revision as a draft", 0.9, "user_explicit"
    )
    memory = MemoryStore(workspace=tmp_path / name, vector_store=vectors)
    memory.write_preferences(f"# User Preferences\n\n- {doc}\n")
    return memory


def _builder(tmp_path: Path, memory: MemoryStore) -> ContextBuilder:
    return ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def _block(rendered: str) -> str:
    return rendered.split(HEADER, 1)[1].split("[END REINJECTED]", 1)[0]


def test_a_compaction_brings_back_the_doc_and_the_relevant_pref_rows(tmp_path, opened, home):
    builder = _builder(tmp_path, _store(tmp_path, opened, "ws", DOC_RULE))
    rendered, _ = builder.build_message(QUERY, False, "session-1", needs_reinjection=True)
    block = _block(rendered)
    assert DOC_RULE in block
    assert f"{RELEVANT}: Upload a zircon review revision as a draft" in block
    # Same allowance and omission line as session start: not every row comes back.
    assert f"{_PREFS_STARTUP_CAP}-character startup budget" in block
    assert 0 < block.count("\npref.rule_") < 60
    assert "pref.rule_59:" not in block


def test_a_turn_without_a_compaction_gets_no_block(tmp_path, opened, home):
    builder = _builder(tmp_path, _store(tmp_path, opened, "ws", DOC_RULE))
    rendered, _ = builder.build_message(QUERY, False, "session-1")
    assert HEADER not in rendered


@pytest.mark.parametrize(
    "options", [{"blocks_reads": True}, {"minimal_context": True}, {"context_groups": frozenset()}]
)
def test_withheld_memory_gets_no_block(tmp_path, opened, home, options):
    builder = _builder(tmp_path, _store(tmp_path, opened, "ws", DOC_RULE))
    rendered, _ = builder.build_message(
        QUERY, False, "session-1", needs_reinjection=True, **options
    )
    assert HEADER not in rendered
    assert DOC_RULE not in rendered


def test_a_slim_resume_brings_them_back(tmp_path, opened, home):
    builder = _builder(tmp_path, _store(tmp_path, opened, "ws", DOC_RULE))
    rendered, _ = builder.build_message(QUERY, True, "dashboard:s1", resumed=True, agent="kirocrew")
    assert "[SESSION RESUMED" in rendered
    block = _block(rendered)
    assert DOC_RULE in block and RELEVANT in block


def test_a_named_store_session_gets_its_own_store(tmp_path, opened, home):
    workspace = _store(tmp_path, opened, "ws", "WORKSPACE_ONLY_RULE")
    own = _store(tmp_path, opened, "bot", DOC_RULE)
    builder = _builder(tmp_path, workspace)
    asked: list[str | None] = []

    def _memory_for(_workspace, memory_store=None):
        asked.append(memory_store)
        return own if memory_store == "bot-store" else workspace

    builder.get_memory_for = _memory_for  # type: ignore[method-assign]
    rendered, _ = builder.build_message(
        QUERY, False, "session-1", needs_reinjection=True, memory_store="bot-store"
    )
    block = _block(rendered)
    assert DOC_RULE in block
    assert "WORKSPACE_ONLY_RULE" not in rendered
    assert "bot-store" in asked


def test_a_private_member_keeps_its_own_store_and_gets_no_second_copy(env):
    """A private member's own preferences ride its essentials on every turn, from
    its own store; session start reads it no workspace preferences, so neither
    does the re-injection."""
    message, _ = env.builder.build_message(
        "Continue",
        False,
        "cron:member-task",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        needs_reinjection=True,
    )
    assert message.count("Preference anchor: 请保留中文原文。") == 1
    assert HEADER not in message
