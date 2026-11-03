"""A doc session's conductor gets its doc's standing brief in the contract, once.

The page's turns carry only what changed (the ask, a rename, a new copy of the doc),
so the conventions those turns follow live in the ``[AGENT SYSTEM PROMPT]`` block,
which a session gets at its start and back after a compaction. The brief names the
doc by id alone, so the block stays identical across a session's renders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import doc_session_brief
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import sections
from kiro_crew.dashboard.doc_files import doc_kind, mint_doc_id
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_SHIPPED_PROMPT = Path(sections.__file__).resolve().parents[1] / "config" / "prompt.md"
_KEY = "dashboard:doc-slot"


@pytest.fixture(autouse=True)
def _fresh_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    # Process-local state: each test starts with no session pinned.
    monkeypatch.setattr(doc_session_brief, "_PINNED", {})


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: _SHIPPED_PROMPT)
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


def _resolve(builder: ContextBuilder, *, start: bool = True) -> str:
    return builder._resolve_agent_prompt(
        None,
        project=None,
        mode="",
        session_key=_KEY,
        is_cc=False,
        private_owner=False,
        session_start=start,
    )


def test_a_doc_session_contract_carries_its_brief_and_restores_it_unchanged(
    builder: ContextBuilder,
) -> None:
    doc_id = mint_doc_id("doc")
    doc_session_brief.pin_doc_session(_KEY, doc_id)
    started = _resolve(builder)
    assert "## This session is a document's conductor" in started
    assert f'slug "doc-overlay-{doc_id}"' in started
    assert "`[Doc ask <id>]`" in started
    assert "`[Doc title]" in started
    # The base contract is still there, ahead of the brief.
    assert started.index("Kiro Crew") < started.index("document's conductor")
    # The reinjection after a compaction renders the same block.
    assert _resolve(builder, start=False) == started


def test_no_brief_for_an_ordinary_session_or_another_page_kind(builder: ContextBuilder) -> None:
    plain = _resolve(builder)
    assert "document's conductor" not in plain
    doc_session_brief.pin_doc_session(_KEY, mint_doc_id("board"))
    assert _resolve(builder) == plain
    doc_session_brief.pin_doc_session(_KEY, None)
    assert doc_session_brief.pinned_doc(_KEY) is None


def test_the_brief_names_the_doc_by_id_only() -> None:
    doc_id = mint_doc_id("doc")
    brief = doc_session_brief.doc_brief(doc_id)
    assert doc_id in brief
    assert brief == doc_session_brief.doc_brief(doc_id)
    assert "Untitled" not in brief


@pytest.mark.parametrize("kind", ["doc", "board", "codebase", "dashboard"])
def test_doc_kind_reads_back_the_kind_an_id_was_minted_for(kind: str) -> None:
    assert doc_kind(mint_doc_id(kind)) == kind


def test_a_plain_page_gets_the_worker_brief_and_a_crew_mode_page_the_conductor_s(
    builder: ContextBuilder,
) -> None:
    doc_id = mint_doc_id("doc")
    doc_session_brief.pin_doc_session(_KEY, doc_id, conductor=False)
    plain = _resolve(builder)
    assert "do it yourself, in this session" in plain
    assert "Never open another session for it" in plain
    assert "git worktree" in plain
    assert "Start workers with session_create" not in plain
    assert "### Being opened by another page" in plain
    doc_session_brief.pin_doc_session(_KEY, doc_id, conductor=True)
    crew = _resolve(builder)
    assert "Start workers with session_create" in crew
    assert "do it yourself, in this session" not in crew
    assert "### Opening another page" in crew
