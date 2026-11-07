"""An agent spec's non-gating hooks run on codex, fired by Crew's turn loop.

codex-acp's ``session/new`` has no hooks field and codex reads no Kiro spec, so
nothing runs the spec's ``hooks`` there unless Crew does. Under the ``read-only``
mode Crew sets, codex runs reads and sandboxed commands without a permission
request, so a ``preToolUse`` hook could not gate them and codex stays out of
``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``. The other four events need no permission
request, so codex is a member of ``ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS``.
These tests pin:

* the membership, and the card line that names only ``preToolUse`` as lost;
* the chat turn takes the four events, and a ``preToolUse`` entry keeps a notice;
* nothing on codex gates, so a spec that cannot be read blocks no tool call;
* a codex shell call, in the shape codex-acp 2.3.0 sent it live, is named
  ``execute_bash``, and a codex MCP call, which codex-acp also sends as kind
  ``execute``, is not: it meets its ``@server/tool`` matcher instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.acp import _dispatch
from kiro_crew.acp.types import EVENT_TOOL_CALL
from kiro_crew.agent_sdk import backend_mcp_ability, spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
    ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookResult,
    ScriptHookStore,
    pre_tool_match_names,
)

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "codex"

_ALL_EVENTS = {
    "agentSpawn": [{"command": "spawn.sh"}],
    "userPromptSubmit": [{"command": "prompt.sh"}],
    "preToolUse": [{"matcher": "execute_bash", "command": "guard.sh"}],
    "postToolUse": [{"matcher": "execute_bash", "command": "post.sh"}],
    "stop": [{"command": "stop.sh"}],
}

_PRE_TOOL_NOTICE = "does not run an agent's preToolUse hooks"
_UNREAD_NOTICE = "could not be read, so none run in this session"
_RUN_NOWHERE = "does not run an agent's hooks,"


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: seen.append(text),
    )
    return seen


def _write_spec(agents_dir: Path, name: str, hooks: object) -> None:
    spec = {"name": name, "prompt": "p", "hooks": hooks}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _client(cwd: str = "") -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CODEX), cwd=cwd)


def _prepare(client, agent, *, is_new=False):
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), client, agent, is_new=is_new
        )
    )


def _fake_runs(monkeypatch) -> list:
    ran: list = []

    async def fake_run(hook, context="", hook_event=None, cwd=None):
        ran.append((hook.event, hook.command, (hook_event or {}).get("tool_name")))
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=0)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    return ran


# ── membership and the card ──


def test_codex_fires_the_observer_events_only():
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    caps = capabilities_for(ACP_BACKEND_CODEX)
    assert caps.crew_fires_spec_observer_hooks is True
    assert caps.crew_fires_spec_hooks is False


def test_the_codex_card_says_only_pre_tool_use_hooks_do_not_run():
    listed = backend_mcp_ability.ability_payload(ACP_BACKEND_CODEX)["ineffective"]
    assert backend_mcp_ability.HOOKS_PRE_TOOL_USE_CARD_ID in listed
    assert "hooks" not in listed
    assert spec_hooks.spec_hooks_run_nowhere(_client()) is False


# ── the chat turn ──


def test_a_codex_turn_gets_every_spec_hook_but_pre_tool_use(agents_dir, notices):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    hooks, unreadable, cwd = _prepare(_client("/w"), "a1")
    assert unreadable is False
    assert cwd == "/w"
    assert [(h.event, h.command) for h in hooks] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "prompt.sh"),
        (HOOK_EVENT_POST_TOOL_USE, "post.sh"),
        (HOOK_EVENT_STOP, "stop.sh"),
    ]
    assert notices == []


def test_a_new_codex_session_hears_only_that_pre_tool_use_does_not_run(agents_dir, notices):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    _prepare(_client(), "a1", is_new=True)
    assert len(notices) == 1
    assert "Agent a1 has hooks on preToolUse." in notices[0]
    assert _PRE_TOOL_NOTICE in notices[0]
    assert _RUN_NOWHERE not in notices[0]


def test_a_codex_spec_with_only_prompt_and_stop_hooks_gets_no_notice(agents_dir, notices):
    """The marker-stamping shape a gated workflow uses: it all runs, so nothing is said."""
    _write_spec(
        agents_dir,
        "a1",
        {"userPromptSubmit": [{"command": "stamp.sh"}], "stop": [{"command": "done.sh"}]},
    )
    hooks, _, _ = _prepare(_client(), "a1", is_new=True)
    assert [h.command for h in hooks] == ["stamp.sh", "done.sh"]
    assert notices == []


def test_an_unreadable_spec_blocks_no_codex_tool_call(agents_dir, notices):
    """Nothing on codex gates on the spec, so a bad spec costs its hooks only."""
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    assert _prepare(_client(), "a1", is_new=True) == ([], False, None)
    assert len(notices) == 1 and _UNREAD_NOTICE in notices[0]


# ── codex tool calls, as codex-acp 2.3.0 sent them live ──


def _live_tool_call(fixture: str):
    """The first ``tool_call`` frame of a live codex capture, parsed as the turn parses it."""
    caches: dict = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "diff_path_cache": {},
        "harness_tool_name_cache": {},
    }
    for line in (_FRAMES / fixture).read_text(encoding="utf-8").splitlines():
        update = json.loads(line).get("params", {}).get("update", {})
        if update.get("sessionUpdate") == "tool_call":
            (started,) = _dispatch.parse_session_update(update, **caches)
            assert started.kind == EVENT_TOOL_CALL
            return started
    raise AssertionError(f"{fixture} has no tool_call frame")


def _post_tool_fire(tmp_path, hooks, started) -> None:
    """Fire PostToolUse as the chat turn does for a finished call."""
    identity = spec_hooks.post_tool_identity(_client(), started, is_shell=bool(started.is_shell))
    assert identity is not None
    _all, spec_names = pre_tool_match_names(
        started.title,
        tool_identity=identity.tool_identity,
        mcp_server=identity.mcp_server,
        harness_tool_id=identity.tool_id,
    )
    asyncio.run(
        ScriptHookStore(config_dir=tmp_path).fire(
            HOOK_EVENT_POST_TOOL_USE,
            tool_name=started.title,
            tool_response={"output": "ok"},
            extra_hooks=hooks,
            extra_hooks_tool_names=spec_names,
            tool_match_names=_all,
        )
    )


_SHELL_AND_MCP_POST_HOOKS = {
    "postToolUse": [
        {"matcher": "execute_bash", "command": "post.sh"},
        {"matcher": "@probe/probe_codeword", "command": "mcp.sh"},
    ]
}


def test_a_codex_shell_call_fires_an_execute_bash_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    _write_spec(agents_dir, "a1", _SHELL_AND_MCP_POST_HOOKS)
    hooks, _, _ = _prepare(_client(), "a1")
    started = _live_tool_call("session-live.jsonl")
    assert started.is_shell is True
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started)
    assert ran == [(HOOK_EVENT_POST_TOOL_USE, "post.sh", "execute_bash")]


def test_a_codex_mcp_call_is_not_named_execute_bash(agents_dir, notices, tmp_path, monkeypatch):
    """codex-acp sends an MCP call as kind ``execute``; its marker keeps it off the shell."""
    _write_spec(agents_dir, "a1", _SHELL_AND_MCP_POST_HOOKS)
    hooks, _, _ = _prepare(_client(), "a1")
    started = _live_tool_call("saved-spec-projection-live.jsonl")
    assert started.is_shell is False
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started)
    assert [command for _event, command, _name in ran] == ["mcp.sh"]


# ── subagent and task-runner turns ──


def test_a_codex_subagent_turn_gets_the_observer_hooks_ungated(agents_dir):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client("/w"), "a1"))
    assert spec.gated is False and spec.unreadable is False and spec.cwd == "/w"
    assert HOOK_EVENT_PRE_TOOL_USE not in {h.event for h in spec.hooks}
    assert {h.command for h in spec.hooks} == {"spawn.sh", "prompt.sh", "post.sh", "stop.sh"}


def test_an_unreadable_spec_leaves_a_codex_subagent_turn_ungated(agents_dir):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client(), "a1"))
    assert spec == spec_hooks.TurnSpecHooks([], None, False, False)
