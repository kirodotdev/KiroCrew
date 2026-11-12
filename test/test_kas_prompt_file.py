"""KAS reads a ``file://`` prompt the way kiro-cli does.

kiro-cli expands ``$VAR`` in the path and runs an agent whose prompt file is
missing or empty with no prompt (``agent_config/load.rs`` ``resolve_global_prompt``),
and reads ``systemPrompt`` as ``prompt``. Crew resolves the prompt for KAS, so it
has to do the same: run the agent on KAS's default prompt and say so in a notice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_agents
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_agents import (
    KasAgentTranslationError,
    build_kas_custom_agents,
    resolve_prompt,
)
from kiro_crew.agent_sdk.spec_hooks import kas_spec_load_error


@pytest.mark.parametrize("ref", ["file://$N9_PROMPTS/p.md", "file://${N9_PROMPTS}/p.md"])
def test_a_prompt_path_expands_an_env_var(monkeypatch, tmp_path, ref):
    (tmp_path / "p.md").write_text("from env path", encoding="utf-8")
    monkeypatch.setenv("N9_PROMPTS", str(tmp_path))

    text = resolve_prompt({"prompt": ref}, agent_id="a", agents_dir=tmp_path / "agents")

    assert text == "from env path"


def test_system_prompt_is_read_when_prompt_is_absent(tmp_path):
    assert resolve_prompt({"systemPrompt": "sp"}, agent_id="a", agents_dir=tmp_path) == "sp"
    both = {"prompt": "p", "systemPrompt": "sp"}
    assert resolve_prompt(both, agent_id="a", agents_dir=tmp_path) == "p"


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_a_missing_or_empty_prompt_file_runs_on_the_default_prompt(tmp_path, content):
    if content is not None:
        (tmp_path / "p.md").write_text(content, encoding="utf-8")
    spec = {"name": "a", "prompt": "file://./p.md"}
    # Every other reader still refuses: a crewmate copy must not inline "".
    with pytest.raises(kas_agents.KasPromptFileUnreadable):
        resolve_prompt(spec, agent_id="a", agents_dir=tmp_path)

    unread: list[Path] = []
    [entry] = build_kas_custom_agents(tmp_path, "a", spec, prompt_unread_out=unread)

    assert entry["prompt"] == ""
    assert unread == [(tmp_path / "p.md").resolve()]


def test_a_prompt_path_that_cannot_expand_runs_on_the_default_prompt(tmp_path):
    spec = {"name": "a", "prompt": "file://~nosuchuser_n9/p.md"}
    unread: list[Path] = []

    [entry] = build_kas_custom_agents(tmp_path, "a", spec, prompt_unread_out=unread)

    assert entry["prompt"] == ""
    assert len(unread) == 1


def test_a_disallowed_prompt_path_still_refuses(tmp_path):
    spec = {"name": "a", "prompt": "file:///proc/self/environ"}
    with pytest.raises(KasAgentTranslationError) as raised:
        build_kas_custom_agents(tmp_path, "a", spec, prompt_unread_out=[])
    assert not isinstance(raised.value, kas_agents.KasPromptFileUnreadable)


def test_the_kas_load_probe_accepts_a_missing_prompt_file(tmp_path):
    (tmp_path / "a.json").write_text(
        json.dumps({"name": "a", "prompt": "file://./gone.md"}), encoding="utf-8"
    )
    assert kas_spec_load_error(tmp_path, "a") is None


@pytest.mark.asyncio
async def test_a_session_on_a_missing_prompt_file_starts_with_a_notice(monkeypatch, tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text(
        json.dumps({"name": "a", "prompt": "file://./gone.md"}), encoding="utf-8"
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)

    extras = await KasHarness().session_extras("a", work_dir=str(tmp_path))

    assert extras.custom_agents[0]["prompt"] == ""
    assert "gone.md" in extras.withheld_credentials_notice
    assert "default prompt" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_resumed_session_keeps_the_notice(monkeypatch, tmp_path):
    # A resumed chat is a new client too (``is_new``), and it reads the notice
    # off the handle that ``session/load`` built.
    from acp_runtime_helpers import _make_runtime

    from kiro_crew.acp.harness import SessionExtras
    from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    rt, _, _ = _make_runtime()
    rt._can_load_session = True
    rt._acp_backend = ACP_BACKEND_KAS

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "a"}, "models": []}
        return {}

    async def _fake_agents(agent, **_kw):
        return SessionExtras(
            custom_agents=[{"id": agent, "prompt": "", "tools": []}],
            withheld_credentials_notice="the prompt notice",
        )

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)

    handle = await rt.load_session("", "sid-n9", cwd=str(tmp_path), agent="a", session_key="k")

    assert handle.kas_withheld_credentials_notice == "the prompt notice"
