"""A custom agent's prompt reaches a KAS session once (kirodotdev/KiroCrew#13305).

KAS is handed the agent's own prompt on its wire definition, and the runtime
records that text under ``template://<agent>#prompt``. The session-start
``[AGENT SYSTEM PROMPT]`` block that would repeat it word for word is withheld;
any other text, and the turn after a compaction, keep the block.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import agent_sdk
from kiro_crew.acp.types import PROVIDER_LABEL_KAS
from kiro_crew.context import ContextBuilder
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

PERSONA = "You are the release-notes writer. Keep every entry to one line."
BLOCK = "[AGENT SYSTEM PROMPT]"


def _write_spec(tmp_path, monkeypatch, prompt: str) -> None:
    agents_dir = tmp_path / ".kiro" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "writer.json").write_text(
        json.dumps({"name": "writer", "prompt": prompt}), encoding="utf-8"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents_dir)
    # Admit the provider double below, as the real provider classes are admitted.
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)


def _kas_provider(recorded: dict[str, str]):
    """A KAS session whose runtime recorded *recorded* at activation; no process."""
    return SimpleNamespace(
        essential_delivery=EssentialDelivery(),
        context_provider_type=PROVIDER_LABEL_KAS,
        cwd="",
        native_context_documents=dict(recorded),
        native_steering=True,
        member_dispatch_mounted=None,
        served_model="",
    )


def _turn(tmp_path, provider, *, fresh: bool = True) -> str:
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    msg, _ = builder.build_message(
        "hello",
        fresh,
        "cli:writer",
        agent="writer",
        context_provider=provider,
        needs_reinjection=not fresh,
    )
    return msg


@pytest.mark.parametrize("spelling", ["inline", "file"])
def test_a_kas_session_handed_the_prompt_gets_no_second_copy(tmp_path, monkeypatch, spelling):
    persona_file = tmp_path / "persona.md"
    persona_file.write_text(PERSONA, encoding="utf-8")
    _write_spec(
        tmp_path, monkeypatch, PERSONA if spelling == "inline" else f"file://{persona_file}"
    )
    # Control: with nothing recorded, the spec's prompt resolves to the persona.
    assert f"{BLOCK}\n{PERSONA}\n" in _turn(tmp_path, _kas_provider({}))
    msg = _turn(tmp_path, _kas_provider({"template://writer#prompt": PERSONA}))
    assert BLOCK not in msg
    assert PERSONA not in msg


@pytest.mark.parametrize(
    ("prompt", "recorded"),
    [
        (PERSONA, {}),
        (PERSONA, {"template://other#prompt": PERSONA}),
        # The harness holds the literal token; the block carries the resolved name.
        ("You are {bot_name}.", {"template://writer#prompt": "You are {bot_name}."}),
    ],
    ids=["nothing-recorded", "another-agent", "resolved-differs"],
)
def test_the_block_stays_unless_the_harness_holds_its_exact_text(
    tmp_path, monkeypatch, prompt, recorded
):
    _write_spec(tmp_path, monkeypatch, prompt)
    msg = _turn(tmp_path, _kas_provider(recorded))
    assert msg.count(BLOCK) == 1


def test_the_turn_after_a_compaction_still_restores_the_block(tmp_path, monkeypatch):
    _write_spec(tmp_path, monkeypatch, PERSONA)
    provider = _kas_provider({"template://writer#prompt": PERSONA})
    msg = _turn(tmp_path, provider, fresh=False)
    assert f"{BLOCK}\n{PERSONA}\n[END AGENT SYSTEM PROMPT]" in msg
