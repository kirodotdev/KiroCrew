"""An agent spec's non-gating hooks run on MiMo Code, fired by Crew's turn loop.

``mimo acp`` is handed no Kiro spec and its ``session/new`` has no hooks field, so
nothing runs the spec's ``hooks`` there unless Crew does. Some MiMo Code tools never
reach its permission service (``memory``, ``history``, ``task``, an edit under its
memory directory, ``question`` and ``plan_exit``), so a ``preToolUse`` hook could not
gate them and mimocode stays out of ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``. The other
four events need no permission request, so mimocode is a member of
``ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS``. These tests pin:

* the membership, and the card line that names only ``preToolUse`` as lost;
* the chat turn takes the four events, a ``preToolUse`` entry keeps a notice, and a
  spec that cannot be read blocks no tool call;
* a call replayed from a live 0.1.15 capture, named on its permission request
  through MiMo Code's row in ``HARNESS_TOOL_TABLES``, meets its kiro-cli matcher: a
  shell call is ``execute_bash``, ``notebook_edit`` is ``fs_write``, ``actor`` is
  ``use_subagent``, and an MCP call meets its ``@server/tool`` matcher and not the
  shell one;
* a call that never asks (``memory``) is matched by its title alone;
* cron and subagent turns take the same four events, ungated.
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
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL, JsonRpcMessage
from kiro_crew.agent_sdk import backend_mcp_ability, spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_MIMOCODE,
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

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode"

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
    return SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_MIMOCODE), cwd=cwd)


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


def test_mimocode_fires_the_observer_events_only():
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS
    assert ACP_BACKEND_MIMOCODE not in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    caps = capabilities_for(ACP_BACKEND_MIMOCODE)
    assert caps.crew_fires_spec_observer_hooks is True
    assert caps.crew_fires_spec_hooks is False


def test_the_mimocode_card_says_only_pre_tool_use_hooks_do_not_run():
    listed = backend_mcp_ability.ability_payload(ACP_BACKEND_MIMOCODE)["ineffective"]
    assert backend_mcp_ability.HOOKS_PRE_TOOL_USE_CARD_ID in listed
    assert "hooks" not in listed
    assert spec_hooks.spec_hooks_run_nowhere(_client()) is False


# ── the chat turn ──


def test_a_mimocode_turn_gets_every_spec_hook_but_pre_tool_use(agents_dir, notices):
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


def test_a_new_mimocode_session_hears_only_that_pre_tool_use_does_not_run(agents_dir, notices):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    _prepare(_client(), "a1", is_new=True)
    assert len(notices) == 1
    assert "Agent a1 has hooks on preToolUse." in notices[0]
    assert _PRE_TOOL_NOTICE in notices[0]
    assert _RUN_NOWHERE not in notices[0]


def test_a_mimocode_spec_with_only_prompt_and_stop_hooks_gets_no_notice(agents_dir, notices):
    _write_spec(
        agents_dir,
        "a1",
        {"userPromptSubmit": [{"command": "stamp.sh"}], "stop": [{"command": "done.sh"}]},
    )
    hooks, _, _ = _prepare(_client(), "a1", is_new=True)
    assert [h.command for h in hooks] == ["stamp.sh", "done.sh"]
    assert notices == []


def test_an_unreadable_spec_blocks_no_mimocode_tool_call(agents_dir, notices):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    assert _prepare(_client(), "a1", is_new=True) == ([], False, None)
    assert len(notices) == 1 and _UNREAD_NOTICE in notices[0]


# ── calls replayed from the live 0.1.15 captures ──


def _caches() -> dict:
    return {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "diff_path_cache": {},
        "harness_tool_name_cache": {},
    }


def _replayed_call(fixture: str, call_id: str, mcp_servers: tuple[str, ...] = ()):
    """*call_id*'s ``tool_call`` event and permission event, through the real parsers."""
    caches = _caches()
    started = None
    for line in (_FRAMES / fixture).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "session/update":
            update = params.get("update") or {}
            events = _dispatch.parse_session_update(update, **caches)
            if update.get("sessionUpdate") == "tool_call" and update.get("toolCallId") == call_id:
                (started,) = events
                assert started.kind == EVENT_TOOL_CALL
        elif method == "session/request_permission":
            if (params.get("toolCall") or {}).get("toolCallId") != call_id:
                continue
            msg = JsonRpcMessage(id=frame["id"], method=method, params=params)
            asked, _ = _dispatch.build_permission_event(
                msg,
                tool_input_cache=caches["tool_input_cache"],
                tool_input_redacted_cache=caches["tool_input_redacted_cache"],
                shell_cache=caches["shell_cache"],
                raw_params_cache=caches["raw_params_cache"],
                mcp_server_name_cache=caches["mcp_server_name_cache"],
                tool_name_cache=caches["tool_name_cache"],
                diff_path_cache=caches["diff_path_cache"],
                harness_tool_name_cache=caches["harness_tool_name_cache"],
                harness_backend=ACP_BACKEND_MIMOCODE,
                harness_mcp_servers=mcp_servers,
            )
            assert started is not None
            assert asked is not None and asked.kind == EVENT_PERMISSION_REQUEST
            return started, asked
    raise AssertionError(f"{fixture} has no permission request for {call_id}")


def _post_tool_fire(tmp_path, hooks, started, asked=None) -> None:
    """Fire PostToolUse as the chat turn does: the tool_call frame, then the ask."""
    is_shell = bool(started.is_shell)
    identity = spec_hooks.post_tool_identity(_client(), started, is_shell=is_shell)
    if asked is not None:
        identity = spec_hooks.post_tool_identity(
            _client(), asked, is_shell=is_shell, earlier=identity
        )
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


_POST_HOOKS = {
    "postToolUse": [
        {"matcher": "execute_bash", "command": "shell.sh"},
        {"matcher": "fs_write", "command": "write.sh"},
        {"matcher": "use_subagent", "command": "subagent.sh"},
        {"matcher": "@crew-probe/crew_probe_echo", "command": "mcp.sh"},
        {"matcher": "memory", "command": "memory.sh"},
    ]
}


def _fired(agents_dir, tmp_path, monkeypatch, started, asked=None) -> list[str]:
    _write_spec(agents_dir, "a1", _POST_HOOKS)
    hooks, _, _ = _prepare(_client(), "a1")
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started, asked)
    return [command for _event, command, _name in ran]


def test_a_mimocode_shell_call_fires_an_execute_bash_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    started, asked = _replayed_call("session-live.jsonl", "call-1")
    assert started.is_shell is True
    _write_spec(agents_dir, "a1", _POST_HOOKS)
    hooks, _, _ = _prepare(_client(), "a1")
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started, asked)
    # A spec hook's stdin names the call as kiro-cli does (``kiro_cli_hook_payload``).
    assert ran == [(HOOK_EVENT_POST_TOOL_USE, "shell.sh", "shell")]


def test_a_mimocode_notebook_edit_fires_an_fs_write_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    # Its permission request is titled ``edit``; the name comes from the tool_call.
    started, asked = _replayed_call("notebook-edit-live.jsonl", "call-2")
    assert _fired(agents_dir, tmp_path, monkeypatch, started, asked) == ["write.sh"]


def test_a_mimocode_actor_call_fires_a_use_subagent_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    started, asked = _replayed_call("actor-subagent-live.jsonl", "call-1")
    assert _fired(agents_dir, tmp_path, monkeypatch, started, asked) == ["subagent.sh"]


def test_a_mimocode_mcp_call_meets_its_server_tool_matcher_not_the_shell_one(
    agents_dir, notices, tmp_path, monkeypatch
):
    started, asked = _replayed_call("mcp-stdio-mount-live.jsonl", "call-1", ("crew-probe",))
    assert started.is_shell is False
    assert _fired(agents_dir, tmp_path, monkeypatch, started, asked) == ["mcp.sh"]


def test_a_mimocode_call_that_never_asks_is_matched_by_its_title(
    agents_dir, notices, tmp_path, monkeypatch
):
    """``memory`` raises no permission request, so only its own title names it."""
    caches = _caches()
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-9",
        "title": "memory",
        "kind": "other",
        "status": "pending",
        "rawInput": {},
    }
    (started,) = _dispatch.parse_session_update(update, **caches)
    assert _fired(agents_dir, tmp_path, monkeypatch, started) == ["memory.sh"]


# ── cron and subagent turns ──


def test_the_specs_prompt_and_stop_hooks_fire_on_a_mimocode_cron_turn(agents_dir, monkeypatch):
    from kiro_crew.cron_service.turn_hooks import CronTurnHooks
    from kiro_crew.messaging import script_hooks

    _write_spec(
        agents_dir,
        "a1",
        {
            "agentSpawn": [{"command": "spawn.sh"}],
            "userPromptSubmit": [{"command": "submit.sh"}],
            "preToolUse": [{"matcher": "execute_bash", "command": "guard.sh"}],
            "stop": [{"command": "stop.sh"}],
        },
    )
    runs: list = []

    async def fake_run(hook, context="", hook_event=None, cwd=None, **_kw):
        runs.append((hook.event, hook.command))
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=0,
            stdout=f"from {hook.command}",
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    monkeypatch.setattr(script_hooks, "get_global_hook_store", lambda: None)
    provider = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_MIMOCODE), cwd="/w", session_id="acp-1"
    )
    hooks = asyncio.run(CronTurnHooks.prepare(provider, "a1", "cron:j1", "go", fresh_session=True))
    assert hooks.gated is False
    assert asyncio.run(hooks.prompt_context()) == ["from spawn.sh", "from submit.sh"]
    asyncio.run(hooks.stop("done"))
    assert runs == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "submit.sh"),
        (HOOK_EVENT_STOP, "stop.sh"),
    ]


def test_a_mimocode_subagent_turn_gets_the_observer_hooks_ungated(agents_dir):
    _write_spec(agents_dir, "a1", _ALL_EVENTS)
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client("/w"), "a1"))
    assert spec.gated is False and spec.unreadable is False and spec.cwd == "/w"
    assert HOOK_EVENT_PRE_TOOL_USE not in {h.event for h in spec.hooks}
    assert {h.command for h in spec.hooks} == {"spawn.sh", "prompt.sh", "post.sh", "stop.sh"}


def test_an_unreadable_spec_leaves_a_mimocode_subagent_turn_ungated(agents_dir):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client(), "a1"))
    assert spec == spec_hooks.TurnSpecHooks([], None, False, False)
