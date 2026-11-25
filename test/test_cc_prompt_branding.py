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
        provider_type="claude_code" if is_cc else None,
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


def _resolve_label(builder: ContextBuilder, provider_type: str | None) -> str:
    return builder._resolve_agent_prompt(
        "kirocrew",
        project=None,
        mode="",
        session_key="sess-cc-branding",
        private_owner=False,
        session_start=True,
        provider_type=provider_type,
    )


@pytest.mark.parametrize("label", [None, "", "acp", "kas", "codex", "label-nobody-registered"])
def test_only_the_claude_label_gets_the_respelled_persona(
    home: Path, builder: ContextBuilder, label: str | None
) -> None:
    (home / "prompt.md").write_text("Kiro Crew runs kiro-cli.\n", encoding="utf-8")
    assert _resolve_label(builder, "claude_code").startswith("Kiro Crew runs claude code.")
    assert not _resolve_label(builder, label).startswith("Kiro Crew runs claude code.")


def test_the_persona_follows_the_capability_not_the_label(
    home: Path, builder: ContextBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give codex ``crew_sends_persona_prompt`` and a codex session gets the persona."""
    import dataclasses

    import kiro_crew.agent_sdk.capabilities as caps_mod

    real = caps_mod.capabilities_for

    def _codex_sends(backend: str):
        record = real(backend)
        if backend == "codex":
            return dataclasses.replace(record, crew_sends_persona_prompt=True)
        return record

    monkeypatch.setattr(caps_mod, "capabilities_for", _codex_sends)
    (home / "prompt.md").write_text("Kiro Crew runs kiro-cli.\n", encoding="utf-8")
    assert _resolve_label(builder, "codex").startswith("Kiro Crew runs claude code.")
