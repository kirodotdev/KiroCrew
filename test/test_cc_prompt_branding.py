"""The default agent's prompt on the claude backend keeps the product name and paths.

``_resolve_agent_prompt`` hands the claude harness the same ``prompt.md`` the
Kiro harness reads. Only the ``kiro-cli`` transport name is respelled; "Kiro
Crew" and the ``~/.kiro/crew`` data home are the same on every backend, so a
rewrite of them would tell the agent about a product that does not exist and
send it looking for skills under a ``~/.claude/crew`` tree nothing writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private data home, so no user override reaches the shipped prompt."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


@pytest.fixture
def builder(tmp_path: Path, opened, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    # The two renders are compared byte for byte, so the host-sized
    # ``{{MAX_SUBAGENTS}}`` figure must not move between them.
    monkeypatch.setattr(ContextBuilder, "_live_cap_figure", staticmethod(lambda: "4"))
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=opened(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)),
    )


def _resolve(builder: ContextBuilder, *, is_cc: bool) -> str:
    return builder._resolve_agent_prompt(
        "kirocrew",
        project=None,
        mode="",
        session_key="sess-cc-branding",
        is_cc=is_cc,
        private_owner=False,
        session_start=True,
    )


def test_shipped_prompt_keeps_kiro_crew_and_its_data_home_paths(
    home: Path, builder: ContextBuilder
) -> None:
    resolved = _resolve(builder, is_cc=True)

    assert "Kiro Crew" in resolved
    assert "~/.kiro/crew/skills/" in resolved
    assert "Claude Crew" not in resolved
    assert "~/.claude/crew" not in resolved
    assert "kiro-cli" not in resolved


def test_claude_prompt_differs_from_the_kiro_prompt_only_in_the_cli_name(
    home: Path, builder: ContextBuilder
) -> None:
    kiro = _resolve(builder, is_cc=False)
    claude = _resolve(builder, is_cc=True)

    assert kiro
    # The claude branch strips the file's trailing newline; the kiro one keeps it.
    assert claude.rstrip() == kiro.replace("kiro-cli", "claude code").rstrip()


def test_user_override_respells_kiro_cli_and_nothing_else(
    home: Path, builder: ContextBuilder
) -> None:
    (home / "prompt.md").write_text(
        "Kiro Crew runs kiro-cli; skills live in ~/.kiro/crew/skills/.\n", encoding="utf-8"
    )

    resolved = _resolve(builder, is_cc=True)

    assert resolved.startswith("Kiro Crew runs claude code; skills live in ~/.kiro/crew/skills/.")
