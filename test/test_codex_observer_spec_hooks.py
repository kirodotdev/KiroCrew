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
  ``execute``, is not: it meets its ``@server/tool`` matcher instead;
* each codex call that raises no permission request is named on its own
  ``tool_call`` event through codex's row in ``HARNESS_TOOL_TABLES``, read from
  what codex-acp writes and never from the title: a file change is ``fs_write``,
  a read, search or listing codex parsed from a command is ``execute_bash``
  and a hosted web search is ``web_search``. ``view_image`` gets no name: it
  comes as one ``tool_call`` frame, already completed, and no result follows;
* that name grants nothing: the READ_ONLY gate decides a codex request as it does
  with no name;
* a ``postToolUse`` matcher that no built-in codex call meets (``grep``,
  ``glob``, ``use_subagent``, ``fs_read``) is named in a notice, on the chat turn
  and on a subagent turn, which says it still meets an MCP tool with its name.
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
from kiro_crew.acp.types import EVENT_TOOL_CALL, JsonRpcMessage
from kiro_crew.agent_sdk import backend_mcp_ability, spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
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
    # A spec hook's stdin names the call as kiro-cli does (``kiro_cli_hook_payload``).
    assert ran == [(HOOK_EVENT_POST_TOOL_USE, "post.sh", "shell")]


def test_a_codex_mcp_call_is_not_named_execute_bash(agents_dir, notices, tmp_path, monkeypatch):
    """codex-acp sends an MCP call as kind ``execute``; its marker keeps it off the shell."""
    _write_spec(agents_dir, "a1", _SHELL_AND_MCP_POST_HOOKS)
    hooks, _, _ = _prepare(_client(), "a1")
    started = _live_tool_call("saved-spec-projection-live.jsonl")
    assert started.is_shell is False
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started)
    assert [command for _event, command, _name in ran] == ["mcp.sh"]


# ── codex calls that raise no permission request, named on the tool_call event ──
#
# Frames in the shape codex-acp 2.3.0 sent them live under Crew's ``read-only``
# mode (capture ``kinds-live*.json``), parsed with the backend the session handle
# passes. None of these calls asked for permission, so the tool_call event is all
# the turn learns of the call.

_EDIT_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "call_patch_shell",
    "kind": "edit",
    "title": "Editing files",
    "status": "in_progress",
    "content": [
        {
            "type": "diff",
            "path": "/work/added2.txt",
            "oldText": None,
            "newText": "second\n",
            "_meta": {"kind": "add"},
        }
    ],
}

#: A shell command codex parsed as a read, a search or a listing: no rawInput.
_PARSED_COMMAND_FRAMES = [
    {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_read",
        "kind": "read",
        "title": "Read file '/work/README.md'",
        "status": "in_progress",
        "locations": [{"path": "/work/README.md"}],
    },
    {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_search",
        "kind": "search",
        "title": "Search for 'hello' in .",
        "status": "in_progress",
    },
    {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_list",
        "kind": "read",
        "title": "List files",
        "status": "in_progress",
    },
]

_VIEW_IMAGE_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "call_image",
    "name": "view_image",
    "kind": "read",
    "title": "View Image /work/pic.png",
    "status": "completed",
    "locations": [{"path": "/work/pic.png"}],
    "rawInput": {"path": "/work/pic.png"},
    "content": [
        {
            "type": "content",
            "content": {"type": "resource_link", "name": "/work/pic.png", "uri": "/work/pic.png"},
        }
    ],
}

_WEB_SEARCH_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "it_web",
    "kind": "search",
    "title": "Web search: acp protocol",
    "status": "in_progress",
    "rawInput": {
        "type": "webSearch",
        "id": "it_web",
        "query": "acp protocol",
        "action": {"type": "search", "query": "acp protocol", "queries": None},
    },
}


def _shell_frame(command: str) -> dict:
    """A codex ``exec_command`` frame as codex-acp titles it: with the command."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_shell",
        "kind": "execute",
        "title": command,
        "status": "in_progress",
        "rawInput": {"command": command, "cwd": "/work"},
        "content": [{"type": "terminal", "terminalId": "call_shell"}],
        "_meta": {"terminal_info": {"cwd": "/work", "terminal_id": "call_shell"}},
    }


def _codex_tool_call(update: dict, names: dict | None = None):
    """*update* parsed as a codex session handle parses it."""
    (started,) = _dispatch.parse_session_update(
        update,
        harness_tool_name_cache={} if names is None else names,
        harness_backend=ACP_BACKEND_CODEX,
    )
    assert started.kind == EVENT_TOOL_CALL
    return started


def _first_tool_call_frame(fixture: str) -> dict:
    for line in (_FRAMES / fixture).read_text(encoding="utf-8").splitlines():
        update = json.loads(line).get("params", {}).get("update", {})
        if update.get("sessionUpdate") == "tool_call":
            return update
    raise AssertionError(f"{fixture} has no tool_call frame")


def _post_hooks(agents_dir: Path, *matchers: str) -> list:
    """The chat turn's hooks for a spec with one ``postToolUse`` hook per matcher."""
    entries = [{"matcher": m, "command": f"{m}.sh"} for m in matchers]
    _write_spec(agents_dir, "a1", {"postToolUse": entries})
    hooks, _, _ = _prepare(_client(), "a1")
    return hooks


def _fired(agents_dir, tmp_path, monkeypatch, started, *matchers: str) -> list[str]:
    """The matchers whose ``postToolUse`` hook ran when *started* finished."""
    hooks = _post_hooks(agents_dir, *matchers)
    ran = _fake_runs(monkeypatch)
    _post_tool_fire(tmp_path, hooks, started)
    return [command.removesuffix(".sh") for _event, command, _name in ran]


def test_a_codex_file_change_fires_an_fs_write_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    started = _codex_tool_call(_EDIT_FRAME)
    fired = _fired(agents_dir, tmp_path, monkeypatch, started, "fs_write", "execute_bash")
    assert fired == ["fs_write"]
    assert started.harness_tool_id == "codex#apply_patch"


@pytest.mark.parametrize("frame", _PARSED_COMMAND_FRAMES, ids=lambda f: f["toolCallId"])
def test_a_command_codex_parsed_as_a_read_is_a_shell_call(
    agents_dir, notices, tmp_path, monkeypatch, frame
):
    """codex has no read or search tool: the frame is a command it parsed."""
    started = _codex_tool_call(frame)
    fired = _fired(agents_dir, tmp_path, monkeypatch, started, "execute_bash", "fs_read")
    assert fired == ["execute_bash"]
    assert started.harness_tool_id == "codex#exec_command"


def test_a_codex_view_image_gets_no_codex_name():
    """codex-acp sends ``view_image`` as one ``tool_call`` frame, already completed.

    No update follows it, so the turn gets no result to fire a postToolUse hook on,
    and ``fs_read`` is a matcher no built-in call meets instead.
    """
    names: dict = {}
    (started,) = _dispatch.parse_session_update(
        _VIEW_IMAGE_FRAME, harness_tool_name_cache=names, harness_backend=ACP_BACKEND_CODEX
    )
    assert started.kind == EVENT_TOOL_CALL
    assert started.harness_tool_id == ""
    assert names == {"call_image": ""}


def test_a_codex_web_search_fires_a_web_search_post_tool_hook(
    agents_dir, notices, tmp_path, monkeypatch
):
    started = _codex_tool_call(_WEB_SEARCH_FRAME)
    fired = _fired(agents_dir, tmp_path, monkeypatch, started, "web_search", "execute_bash")
    assert fired == ["web_search"]
    assert started.harness_tool_id == "codex#web_search"


def test_a_codex_mcp_call_gets_no_codex_name(agents_dir, notices, tmp_path, monkeypatch):
    """Its ``@server/tool`` identity names it; no codex row reaches it."""
    names: dict = {}
    started = _codex_tool_call(_first_tool_call_frame("saved-spec-projection-live.jsonl"), names)
    assert started.harness_tool_id == ""
    assert set(names.values()) == {""}
    fired = _fired(
        agents_dir, tmp_path, monkeypatch, started, "@probe/probe_codeword", "execute_bash"
    )
    assert fired == ["@probe/probe_codeword"]


@pytest.mark.parametrize("command", ["make", "fs_write"])
def test_a_codex_shell_call_is_never_named_by_its_title(
    agents_dir, notices, tmp_path, monkeypatch, command
):
    """codex titles a command with the command, so a one-word one is not a tool name."""
    names: dict = {}
    started = _codex_tool_call(_shell_frame(command), names)
    assert names == {"call_shell": "exec_command"}
    assert started.harness_tool_id == "codex#exec_command"
    fired = _fired(agents_dir, tmp_path, monkeypatch, started, "fs_write", "execute_bash")
    assert fired == ["execute_bash"]


def _bare_direct_client(backend: str):
    """An ``AcpClient`` with only the tool-call caches, as the mirror tests build one."""
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.types import AcpPromptStats

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = backend
    client._available_mode_ids = []
    client._modes_advertised = False
    client._session_key = ""
    client._agent = ""
    client._tool_call_inputs = {}
    client._tool_call_input_redacted = {}
    client._tool_call_is_shell = {}
    client._tool_call_unclassified = {}
    client._tool_call_mcp_server = {}
    client._tool_call_tool_name = {}
    client._tool_call_harness_tool_name = {}
    client._tool_call_params = {}
    client._tool_call_diff_path = {}
    client._permission_options = {}
    client.last_prompt_stats = AcpPromptStats()
    return client


@pytest.mark.parametrize(
    ("frame", "name"),
    [(_EDIT_FRAME, "apply_patch"), (_shell_frame("make"), "exec_command")],
    ids=["edit", "shell"],
)
def test_the_direct_client_names_a_codex_call_on_its_tool_call_event(frame, name):
    client = _bare_direct_client(ACP_BACKEND_CODEX)
    msg = JsonRpcMessage(method="session/update", params={"sessionId": "s", "update": frame})
    event = client._extract_tool_event(msg)
    assert event.harness_tool_id == f"codex#{name}"
    assert client._tool_call_harness_tool_name == {frame["toolCallId"]: name}


def test_no_other_backend_is_named_on_its_tool_call_event():
    """Every other backend is named on its permission request alone, as before."""
    client = _bare_direct_client(ACP_BACKEND_CLAUDE)
    msg = JsonRpcMessage(
        method="session/update", params={"sessionId": "s", "update": _shell_frame("make")}
    )
    assert client._extract_tool_event(msg).harness_tool_id == ""
    (parsed,) = _dispatch.parse_session_update(
        _shell_frame("make"), harness_tool_name_cache={}, harness_backend=ACP_BACKEND_CLAUDE
    )
    assert parsed.harness_tool_id == ""


# ── the codex name grants nothing ──


class _ScriptedProvider:
    def __init__(self, event) -> None:
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        self._events = [event, LLMEvent(kind=EVENT_COMPLETE, text="")]
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message: str):
        for event in self._events:
            yield event

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id: str) -> None:
        self.rejected.append(request_id)


def _gate(acp_event, policy) -> tuple[list[str], list[str]]:
    """``(approved, rejected)`` when the request goes through the provider gate."""
    from kiro_crew.hooks import HookManager, HooksConfig
    from kiro_crew.llm_helpers import stream_and_collect
    from kiro_crew.providers.acp import AcpProvider

    provider = _ScriptedProvider(AcpProvider._to_llm_event(acp_event))
    asyncio.run(
        stream_and_collect(
            provider,  # type: ignore[arg-type]
            "q",
            approval_policy=policy,
            hooks=HookManager(HooksConfig()),
            retry_transient=False,
        )
    )
    return provider.approved, provider.rejected


def _codex_read_request(*, named: bool, side_read_only: bool):
    """The ask codex-acp sent when the parsed ``cat README.md`` had to leave its sandbox."""
    names: dict = {}
    frame = _PARSED_COMMAND_FRAMES[0]
    if named:
        _codex_tool_call(frame, names)
    else:
        _dispatch.parse_session_update(frame, harness_tool_name_cache=names)
    msg = JsonRpcMessage(
        id=0,
        method="session/request_permission",
        params={
            "sessionId": "s1",
            "toolCall": {
                "toolCallId": "call_read",
                "kind": "execute",
                "title": "Read file",
                "status": "pending",
                "rawInput": {"command": "cat README.md", "cwd": "/work"},
                "locations": [{"path": "/work/README.md"}],
            },
            "options": [
                {"optionId": "allow_once", "name": "Yes, proceed", "kind": "allow_once"},
                {"optionId": "cancel", "name": "No", "kind": "reject_once"},
            ],
        },
    )
    event, _ = _dispatch.build_permission_event(
        msg,
        harness_tool_name_cache=names,
        harness_backend=ACP_BACKEND_CODEX,
        side_read_only=side_read_only,
    )
    assert event is not None
    return event


@pytest.mark.parametrize("side_read_only", [True, False])
def test_a_codex_name_is_no_read_proof_and_changes_no_gate_decision(side_read_only):
    """The codex row names a call for hook matching only.

    No read-only built-in is named on a codex request, so the READ_ONLY gate and
    the deny floor see what they saw before, and every policy decides the
    request as it does with no name.
    """
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    named = _codex_read_request(named=True, side_read_only=side_read_only)
    bare = _codex_read_request(named=False, side_read_only=side_read_only)
    assert named.harness_tool_id == "codex#exec_command"
    assert named.harness_builtin_tool == bare.harness_builtin_tool == ""
    approved, rejected = _gate(named, ToolApprovalPolicy.READ_ONLY)
    assert approved == [] and rejected == [named.request_id]
    for policy in (ToolApprovalPolicy.READ_ONLY, ToolApprovalPolicy.HOOK_BASED):
        assert _gate(named, policy) == _gate(bare, policy)


# ── postToolUse matchers no codex call meets ──

_UNMET_AND_MET_POST_HOOKS = {
    "preToolUse": [{"matcher": "execute_bash", "command": "guard.sh"}],
    "postToolUse": [
        {"matcher": m, "command": "post.sh"}
        for m in (
            "grep",
            "glob",
            "use_subagent",
            "fs_read",
            "fs_write",
            "@probe/probe_codeword",
            "fs_*",
        )
    ],
}

#: What both notices say a matcher no built-in tool meets still does.
_MCP_SAME_NAME = "run only after a call to an MCP tool with the same name."


def test_a_new_codex_session_hears_which_post_tool_matchers_meet_no_call(agents_dir, notices):
    _write_spec(agents_dir, "a1", _UNMET_AND_MET_POST_HOOKS)
    _prepare(_client(), "a1", is_new=True)
    pre, unmet = notices
    assert _PRE_TOOL_NOTICE in pre
    assert "Its other hooks run" not in pre
    assert "has postToolUse hooks for grep, glob, use_subagent, fs_read." in unmet
    assert _MCP_SAME_NAME in unmet
    for met in ("fs_write", "@probe", "fs_*"):
        assert met not in unmet


def test_a_codex_subagent_turn_names_the_post_tool_matchers_that_meet_no_call(agents_dir):
    _write_spec(agents_dir, "a1", _UNMET_AND_MET_POST_HOOKS)
    spec = asyncio.run(spec_hooks.turn_spec_hooks(_client(), "a1"))
    assert spec.lost == (
        "hooks.postToolUse[grep]",
        "hooks.postToolUse[glob]",
        "hooks.postToolUse[use_subagent]",
        "hooks.postToolUse[fs_read]",
    )
    notice = spec_hooks.spec_keys_notice("a1", list(spec.lost))
    assert "has postToolUse hooks for grep, glob, use_subagent, fs_read." in notice
    assert _MCP_SAME_NAME in notice
    assert "no effect" not in notice


def test_only_codex_lists_post_tool_matchers_that_meet_no_call(agents_dir):
    _write_spec(agents_dir, "a1", _UNMET_AND_MET_POST_HOOKS)
    hooks, _, _ = _prepare(_client(), "a1")
    claude = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE), cwd="")
    assert spec_hooks.post_tool_matchers_no_builtin_meets(claude, hooks) == []
    assert spec_hooks.post_tool_matchers_no_builtin_meets(_client(), hooks) == [
        "grep",
        "glob",
        "use_subagent",
        "fs_read",
    ]


def _mcp_frame(server: str, tool: str) -> dict:
    """A codex MCP call, in the shape of the live capture's."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_mcp",
        "kind": "execute",
        "title": f"mcp.{server}.{tool}",
        "status": "in_progress",
        "rawInput": {"server": server, "tool": tool, "arguments": {}},
        "_meta": {"is_mcp_tool_call": True},
    }


def test_a_matcher_no_builtin_meets_still_meets_an_mcp_tool_with_its_name(
    agents_dir, notices, tmp_path, monkeypatch
):
    """What the notice says: the hook runs after an MCP tool with the same name."""
    started = _codex_tool_call(_mcp_frame("docs", "web_fetch"))
    fired = _fired(agents_dir, tmp_path, monkeypatch, started, "web_fetch", "grep")
    assert fired == ["web_fetch"]


# ── cron turns ──


def test_the_specs_prompt_and_stop_hooks_fire_on_a_codex_cron_turn(agents_dir, monkeypatch):
    """A codex cron turn fires agentSpawn, userPromptSubmit and stop, and gates nothing.

    The codex copy of the KAS cron test, read from the spec on disk, so the codex
    membership in ``ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS`` decides it.
    """
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
    # No Hooks-page store in this process: only the spec's own hooks may run.
    monkeypatch.setattr(hooks_mod, "get_global_hook_store", lambda: None)
    monkeypatch.setattr(script_hooks, "get_global_hook_store", lambda: None)
    provider = SimpleNamespace(
        capabilities=capabilities_for(ACP_BACKEND_CODEX), cwd="/w", session_id="acp-1"
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
