"""An agent spec's non-gating hooks run on claude, fired by Crew's turn loop.

claude is handed no agent spec, so nothing runs the spec's ``hooks`` there unless
Crew does. It approves some calls inside the harness without a permission request,
so a ``preToolUse`` hook could not gate those calls, and claude stays out of
``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``. The other four events need no permission
request, so claude is a member of ``ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS``
and Crew fires them there. These tests pin:

* the membership, and that kiro-cli is in neither set (it runs the field itself);
* the chat turn takes the four events, and a ``preToolUse`` entry keeps a notice;
* nothing on claude gates, so a spec that cannot be read blocks no tool call;
* a claude Bash call is named ``execute_bash``, so the default agent's audit hook
  fires on it once, on the finished call.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew
import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.acp import _dispatch
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
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

_ALL_EVENTS = {
    "agentSpawn": [{"command": "spawn.sh"}],
    "userPromptSubmit": [{"command": "prompt.sh"}],
    "preToolUse": [{"matcher": "execute_bash", "command": "guard.sh"}],
    "postToolUse": [{"matcher": "execute_bash", "command": "post.sh"}],
    "stop": [{"command": "stop.sh"}],
}

_PRE_TOOL_NOTICE = "does not run an agent's preToolUse hooks"
_UNREAD_NOTICE = "could not be read, so none run in this session"


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


def _client(backend: str, cwd: str = "") -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend), cwd=cwd)


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


# ── membership ──


def test_claude_fires_the_observer_events_only():
    assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    caps = capabilities_for(ACP_BACKEND_CLAUDE)
    assert caps.crew_fires_spec_observer_hooks is True
    assert caps.crew_fires_spec_hooks is False


def test_every_backend_crew_fires_all_events_for_also_gets_the_observer_events():
    assert ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS <= ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX])
def test_a_harness_outside_the_set_gets_no_observer_hooks_from_crew(backend, agents_dir, notices):
    """kiro-cli runs the field itself; firing it here too would run each hook twice."""
    assert backend not in ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    assert _prepare(_client(backend), "a1")[0] == []
    event = SimpleNamespace(harness_tool_id="", tool_name="", mcp_server_name="")
    assert spec_hooks.post_tool_identity(_client(backend), event, is_shell=True) is None


# ── the chat turn ──


def test_a_claude_turn_gets_every_spec_hook_but_pre_tool_use(agents_dir, notices):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    hooks, unreadable, cwd = _prepare(_client(ACP_BACKEND_CLAUDE, "/w"), "a1")
    assert unreadable is False
    assert cwd == "/w"
    assert [(h.event, h.command) for h in hooks] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "prompt.sh"),
        (HOOK_EVENT_POST_TOOL_USE, "post.sh"),
        (HOOK_EVENT_STOP, "stop.sh"),
    ]
    assert notices == []


def test_a_new_claude_session_hears_only_that_pre_tool_use_does_not_run(agents_dir, notices):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    _prepare(_client(ACP_BACKEND_CLAUDE), "a1", is_new=True)
    assert len(notices) == 1
    assert "Agent a1 has hooks on preToolUse." in notices[0]
    assert _PRE_TOOL_NOTICE in notices[0]
    assert "does not run an agent's hooks," not in notices[0]


def test_no_pre_tool_use_notice_without_a_pre_tool_use_hook(agents_dir, notices):
    _write_spec(agents_dir, "a1", {"stop": [{"command": "stop.sh"}]})
    _prepare(_client(ACP_BACKEND_CLAUDE), "a1", is_new=True)
    assert notices == []


def test_an_unreadable_spec_blocks_no_claude_tool_call(agents_dir, notices):
    """Nothing on claude gates on the spec, so a bad spec costs its hooks only."""
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    assert _prepare(_client(ACP_BACKEND_CLAUDE), "a1", is_new=True) == ([], False, None)
    assert len(notices) == 1 and _UNREAD_NOTICE in notices[0]
    notices.clear()
    assert _prepare(_client(ACP_BACKEND_CLAUDE), "a1") == ([], False, None)
    assert notices == []


def test_a_claude_turn_with_no_agent_named_blocks_nothing(agents_dir, notices):
    assert _prepare(_client(ACP_BACKEND_CLAUDE), "", is_new=True) == ([], False, None)
    assert notices == []


# ── a claude Bash call meets an execute_bash matcher ──


def _claude_bash_call() -> tuple:
    """A claude-agent-acp Bash call, its tool_call frame then its completed update."""
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
    meta = {"claudeCode": {"toolName": "Bash"}}
    call = {
        "sessionUpdate": "tool_call",
        "toolCallId": "toolu_1",
        "title": "`ls -la`",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "ls -la", "description": "List files"},
        "_meta": meta,
    }
    done = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "toolu_1",
        "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "total 0"}}],
        "_meta": meta,
    }
    (started,) = _dispatch.parse_session_update(call, **caches)
    (finished,) = _dispatch.parse_session_update(done, **caches)
    return started, finished


def _post_tool_fire(tmp_path, hooks, started) -> None:
    """Fire PostToolUse as the chat turn does for a finished call."""
    identity = spec_hooks.post_tool_identity(
        _client(ACP_BACKEND_CLAUDE), started, is_shell=bool(started.is_shell)
    )
    assert identity is not None
    _all, spec_names = pre_tool_match_names(
        started.title,
        tool_identity=identity.tool_identity,
        mcp_server=identity.mcp_server,
        harness_tool_id=identity.tool_id,
    )
    store = ScriptHookStore(config_dir=tmp_path)
    asyncio.run(
        store.fire(
            HOOK_EVENT_POST_TOOL_USE,
            tool_name=started.title,
            tool_response={"output": "total 0"},
            extra_hooks=hooks,
            extra_hooks_tool_names=spec_names,
            tool_match_names=_all,
        )
    )


def test_a_claude_bash_call_fires_an_execute_bash_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    hooks, _, _ = _prepare(_client(ACP_BACKEND_CLAUDE), "a1")
    started, finished = _claude_bash_call()
    assert started.kind == EVENT_TOOL_CALL and started.is_shell is True
    assert finished.kind == EVENT_TOOL_RESULT and finished.tool_status == "completed"
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started)
    assert ran == [(HOOK_EVENT_POST_TOOL_USE, "post.sh", "execute_bash")]


def test_the_default_agents_audit_hook_fires_on_a_claude_bash_call(
    agents_dir, notices, tmp_path, monkeypatch
):
    """The bundled ``postToolUse`` hook that appends each shell call to ``audit.log``."""
    defaults = json.loads(
        (Path(kiro_crew.__file__).parent / "config" / "defaults.json").read_text(encoding="utf-8")
    )
    (audit,) = defaults["hooks"]["postToolUse"]
    assert audit["matcher"] == "execute_bash" and "audit.log" in audit["command"]
    default_agent = AGENT_FILENAME[: -len(".json")]
    _write_spec(agents_dir, default_agent, {"postToolUse": [audit]})
    hooks, unreadable, _ = _prepare(_client(ACP_BACKEND_CLAUDE), default_agent)
    assert unreadable is False
    started, _finished = _claude_bash_call()
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started)
    assert ran == [(HOOK_EVENT_POST_TOOL_USE, audit["command"], "execute_bash")]


def test_a_claude_call_that_is_not_shell_keeps_title_matching():
    event = SimpleNamespace(harness_tool_id="", tool_name="", mcp_server_name="")
    identity = spec_hooks.post_tool_identity(_client(ACP_BACKEND_CLAUDE), event, is_shell=False)
    assert identity is not None and identity.tool_id == ""


# ── subagent and task-runner turns ──


def test_a_claude_subagent_turn_gets_the_observer_hooks_ungated(agents_dir):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client(ACP_BACKEND_CLAUDE, "/w"), "a1"))
    assert spec.gated is False and spec.unreadable is False and spec.cwd == "/w"
    assert HOOK_EVENT_PRE_TOOL_USE not in {h.event for h in spec.hooks}
    assert {h.command for h in spec.hooks} == {"spawn.sh", "prompt.sh", "post.sh", "stop.sh"}


def test_an_unreadable_spec_leaves_a_claude_subagent_turn_ungated(agents_dir):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client(ACP_BACKEND_CLAUDE), "a1"))
    assert spec == spec_hooks.TurnSpecHooks([], None, False, False)


def test_a_claude_subagent_turn_with_no_agent_named_is_not_blocked(agents_dir):
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client(ACP_BACKEND_CLAUDE), ""))
    assert spec.unreadable is False and spec.gated is False and spec.hooks == []
