"""Agent spec hooks on goose and opencode: Crew's turn loop runs them.

Neither harness receives the spec's ``hooks`` block (its ``session/new`` element set
has no field for it), so Crew fires it, as it does for KAS. Three things have to
hold for that to be true rather than claimed:

* the backend is a member of ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``, so the chat
  turn loop and the subagent / task-runner gate read the spec at all;
* a PreToolUse matcher in kiro-cli's names (``execute_bash``) meets the harness's
  own tool, which needs the permission event to say which tool it is -- read here
  from the committed LIVE frames of each harness, not from a hand-built frame;
* the hook can BLOCK that call, on the permission request.

claude and codex stay out: each approves some calls inside the harness without
asking, so a PreToolUse hook would be skipped on exactly those.
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
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS,
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
)
from kiro_crew.providers.mirrors import mirror_for
from kiro_crew.providers.mirrors.base import Concern, Disposition

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames"

#: backend -> (the live capture holding a shell call and its permission request,
#: the harness's own name for that shell tool).
_SHELL_CAPTURE = {
    ACP_BACKEND_GOOSE: ("goose/turn-live.jsonl", "shell"),
    ACP_BACKEND_OPENCODE: ("opencode/permission-request-live.jsonl", "bash"),
}

_MIRRORS = sorted(_SHELL_CAPTURE)


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


def _write_spec(agents_dir: Path, name: str, hooks: dict) -> None:
    spec = {"name": name, "prompt": "p", "hooks": hooks}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _provider(backend: str, cwd: str = "/w") -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend), cwd=cwd)


def _permission_event(backend: str, *, as_backend: str | None = None):
    """Replay a live capture through the real parsers; return its permission event.

    The ``tool_call`` frames go through ``parse_session_update`` with the caches a
    session handle owns, then the ``session/request_permission`` frame through
    ``build_permission_event``: the shared-runtime parser. The direct ``AcpClient``
    path that goose and opencode sessions take is covered by
    :func:`test_the_direct_client_names_the_harness_tool_too`.
    """
    capture, _ = _SHELL_CAPTURE[backend]
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
    for line in (_FRAMES / capture).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "session/update":
            _dispatch.parse_session_update(params.get("update") or {}, **caches)
        elif method == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=method, params=params)
            event, _ = _dispatch.build_permission_event(
                msg,
                tool_input_cache=caches["tool_input_cache"],
                tool_input_redacted_cache=caches["tool_input_redacted_cache"],
                shell_cache=caches["shell_cache"],
                raw_params_cache=caches["raw_params_cache"],
                mcp_server_name_cache=caches["mcp_server_name_cache"],
                tool_name_cache=caches["tool_name_cache"],
                diff_path_cache=caches["diff_path_cache"],
                harness_tool_name_cache=caches["harness_tool_name_cache"],
                harness_backend=as_backend if as_backend is not None else backend,
            )
            assert event is not None and event.kind == EVENT_PERMISSION_REQUEST
            return event
    raise AssertionError(f"no permission request in {capture}")


def _fake_runs(monkeypatch, exit_code: int) -> list:
    ran: list = []

    async def fake_run(hook, context="", hook_event=None, cwd=None):
        ran.append((hook.event, hook.command, (hook_event or {}).get("tool_name"), cwd))
        return ScriptHookResult(
            hook_id=hook.id, hook_name=hook.name, event=hook.event, exit_code=exit_code
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    return ran


# ── membership: who Crew fires spec hooks for ──


@pytest.mark.parametrize("backend", _MIRRORS)
def test_crew_fires_the_spec_hooks_of_goose_and_opencode(backend):
    assert backend in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    assert capabilities_for(backend).crew_fires_spec_hooks is True


@pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
def test_claude_and_codex_stay_out_until_their_auto_approved_calls_ask(backend):
    # Each approves some calls inside the harness, which then sends no permission
    # request, so a PreToolUse hook would be skipped on those calls.
    assert backend not in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    hooks = mirror_for(backend).rulings()[Concern.HOOKS]
    assert hooks.disposition is Disposition.NO_CHANNEL


@pytest.mark.parametrize("backend", _MIRRORS)
def test_the_mirror_rules_hooks_translated(backend):
    hooks = mirror_for(backend).rulings()[Concern.HOOKS]
    assert hooks.disposition is Disposition.TRANSLATED
    assert "harness_tool_names" in hooks.reason


# ── the permission event names the harness's own tool ──


@pytest.mark.parametrize("backend", _MIRRORS)
def test_a_live_shell_permission_request_carries_the_harness_tool(backend):
    _, native = _SHELL_CAPTURE[backend]
    event = _permission_event(backend)
    assert event.harness_tool_id == f"{backend}#{native}"
    assert spec_hooks.spec_hook_tool_names(event.harness_tool_id)[0] == "execute_bash"


@pytest.mark.parametrize("backend", _MIRRORS)
def test_a_backend_with_no_table_gets_no_harness_tool_id(backend):
    # The same frames on a backend with no name table build the event as before.
    assert _permission_event(backend, as_backend=ACP_BACKEND_CLAUDE).harness_tool_id == ""


def test_opencodes_later_titles_name_the_command_not_the_tool():
    # Only the first tool_call frame's title is the tool; the in_progress update
    # that follows reuses the title for the command and must not replace it.
    update = {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "bash", "kind": "execute"}
    names: dict = {}
    _dispatch.parse_session_update(update, harness_tool_name_cache=names)
    refine = {**update, "sessionUpdate": "tool_call_update", "title": "rm -rf build"}
    _dispatch.parse_session_update(refine, harness_tool_name_cache=names)
    assert names == {"c1": "bash"}
    # A prose title is not a name at all.
    prose = {**update, "toolCallId": "c2", "title": "shell · echo hi"}
    _dispatch.parse_session_update(prose, harness_tool_name_cache=names)
    assert names["c2"] == ""


# ── the hook fires and blocks on that permission request ──


@pytest.mark.parametrize("backend", _MIRRORS)
@pytest.mark.parametrize("matcher", ["execute_bash", "shell"])
def test_a_pre_tool_use_spec_hook_blocks_the_live_shell_call(
    backend, matcher, agents_dir, tmp_path, monkeypatch
):
    _write_spec(agents_dir, "a1", {"preToolUse": [{"matcher": matcher, "command": "deny.sh"}]})
    ran = _fake_runs(monkeypatch, exit_code=2)
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend), "a1"))
    assert turn.gated and not turn.unreadable and len(turn.hooks) == 1
    event = _permission_event(backend)
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path),
            turn.hooks,
            turn.cwd,
            event.title,
            event.tool_input,
            tool_identity=event.tool_name,
            mcp_server=event.mcp_server_name,
            harness_tool_id=event.harness_tool_id,
        )
    )
    # The hook ran, told the call is kiro-cli's execute_bash, and its exit 2 blocked.
    assert ran == [(HOOK_EVENT_PRE_TOOL_USE, "deny.sh", "execute_bash", "/w")]
    assert reason is not None


@pytest.mark.parametrize("backend", _MIRRORS)
def test_a_pre_tool_use_hook_for_another_tool_leaves_the_shell_call_alone(
    backend, agents_dir, tmp_path, monkeypatch
):
    _write_spec(agents_dir, "a1", {"preToolUse": [{"matcher": "fs_write", "command": "deny.sh"}]})
    ran = _fake_runs(monkeypatch, exit_code=2)
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend), "a1"))
    event = _permission_event(backend)
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path),
            turn.hooks,
            turn.cwd,
            event.title,
            event.tool_input,
            tool_identity=event.tool_name,
            mcp_server=event.mcp_server_name,
            harness_tool_id=event.harness_tool_id,
        )
    )
    assert ran == [] and reason is None


# ── the other four events reach the chat turn loop's fire ──


@pytest.mark.parametrize("backend", _MIRRORS)
def test_the_chat_turn_loop_fires_the_other_four_events(backend, agents_dir, tmp_path, monkeypatch):
    _write_spec(
        agents_dir,
        "a1",
        {
            "agentSpawn": [{"command": "spawn.sh"}],
            "userPromptSubmit": [{"command": "prompt.sh"}],
            "postToolUse": [{"command": "post.sh"}],
            "stop": [{"command": "stop.sh"}],
        },
    )
    ran = _fake_runs(monkeypatch, exit_code=0)
    hooks, unreadable, cwd = asyncio.run(
        chat_runner._prepare_spec_hooks(None, None, _provider(backend), "a1", is_new=False)
    )
    assert not unreadable and cwd == "/w"
    store = ScriptHookStore(config_dir=tmp_path)
    for event in (
        HOOK_EVENT_AGENT_SPAWN,
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        HOOK_EVENT_POST_TOOL_USE,
        HOOK_EVENT_STOP,
    ):
        asyncio.run(store.fire(event, "ctx", extra_hooks=hooks, extra_hooks_cwd=cwd))
    assert [(e, c) for e, c, _, _ in ran] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "prompt.sh"),
        (HOOK_EVENT_POST_TOOL_USE, "post.sh"),
        (HOOK_EVENT_STOP, "stop.sh"),
    ]


def _bare_client(backend: str):
    """An ``AcpClient`` with only the tool-call caches, as the goose tests build one."""
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


@pytest.mark.parametrize("backend", _MIRRORS)
def test_the_direct_client_names_the_harness_tool_too(backend):
    # AcpClient parses tool_call frames on its own path, not parse_session_update.
    capture, native = _SHELL_CAPTURE[backend]
    client = _bare_client(backend)
    event = None
    for line in (_FRAMES / capture).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        method = frame.get("method")
        if method == "session/update":
            client._extract_tool_event(JsonRpcMessage(method=method, params=frame["params"]))
        elif method == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=method, params=frame["params"])
            event = client._build_permission_event(msg)
            break
    assert event is not None
    assert event.harness_tool_id == f"{backend}#{native}"


# ── an MCP tool: mcp__server__tool matchers ──


def _opencode_mcp_permission_event(servers: tuple[str, ...]):
    """opencode's live MCP tool_call, then a permission request for that call.

    The tool_call frame is the committed capture's. That turn ran under a stub model
    with no permission request, so the request is opencode's own live shape from
    ``permission-request-live.jsonl`` pointed at this call's id.
    """
    caches: dict = {"tool_name_cache": {}, "harness_tool_name_cache": {}}
    call_id = ""
    for line in (_FRAMES / "opencode/mcp-directive-call-live.jsonl").read_text().splitlines():
        frame = json.loads(line) if line.strip() else {}
        update = (frame.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") == "tool_call":
            call_id = update["toolCallId"]
            _dispatch.parse_session_update(update, **caches)
            break
    assert call_id
    tool_call = {"toolCallId": call_id, "title": "x", "kind": "other", "rawInput": {}}
    options = [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}]
    msg = JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={"sessionId": "s", "toolCall": tool_call, "options": options},
    )
    event, _ = _dispatch.build_permission_event(
        msg,
        tool_name_cache=caches["tool_name_cache"],
        harness_tool_name_cache=caches["harness_tool_name_cache"],
        harness_backend=ACP_BACKEND_OPENCODE,
        harness_mcp_servers=servers,
    )
    return event


def _blocks(event, matcher, tmp_path, monkeypatch) -> bool:
    # Through the real spec conversion, so a matcher the spec reader drops runs nothing.
    spec_hooks._cache.clear()
    hooks, _ = spec_hooks._convert(
        "a1", {"hooks": {"preToolUse": [{"matcher": matcher, "command": "d"}]}}
    )
    ran = _fake_runs(monkeypatch, exit_code=2)
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path),
            list(hooks),
            None,
            event.title,
            event.tool_input,
            tool_identity=event.tool_name,
            mcp_server=event.mcp_server_name,
            harness_tool_id=event.harness_tool_id,
        )
    )
    assert (reason is not None) is bool(ran)
    return bool(ran)


@pytest.mark.parametrize(
    "matcher",
    ["mcp__kirocrew-core__monitor_start", "mcp__kirocrew-core__*", "kirocrew-core_monitor_start"],
)
def test_an_opencode_mcp_deny_hook_meets_the_fused_tool(matcher, tmp_path, monkeypatch):
    event = _opencode_mcp_permission_event(("kirocrew-core", "other"))
    assert event.harness_tool_id.split("|")[0] == "opencode#@kirocrew-core/monitor_start"
    assert _blocks(event, matcher, tmp_path, monkeypatch)
    assert not _blocks(event, "mcp__other__*", tmp_path, monkeypatch)


@pytest.mark.parametrize("matcher", ["@kirocrew-core/monitor_start", "@kirocrew-core/*"])
def test_an_at_server_slash_tool_matcher_meets_the_fused_tool(matcher, tmp_path, monkeypatch):
    # The documented MCP spelling the split already produces: Crew-fired spec
    # hooks accept it beside the object form's rule, scoped to its own server.
    event = _opencode_mcp_permission_event(("kirocrew-core", "other"))
    assert _blocks(event, matcher, tmp_path, monkeypatch)
    assert not _blocks(event, "@other/*", tmp_path, monkeypatch)


def test_a_server_whose_name_prefixes_a_native_tool_keeps_the_native_names(tmp_path, monkeypatch):
    # Server "apply" + opencode's own apply_patch: both readings are kept, so an
    # fs_write deny still meets the native write, and an MCP deny meets the split.
    names = spec_hooks.spec_hook_tool_names(_qualified("opencode", "apply_patch", ("apply",)))
    assert names is not None
    assert {"fs_write", "write", "apply_patch", "@apply/patch", "mcp__apply__patch"} <= set(names)
    event = SimpleNamespace(
        title="apply_patch",
        tool_input=None,
        tool_name="",
        mcp_server_name="",
        harness_tool_id=_qualified("opencode", "apply_patch", ("apply",)),
    )
    assert _blocks(event, "fs_write", tmp_path, monkeypatch)
    assert _blocks(event, "mcp__apply__*", tmp_path, monkeypatch)


@pytest.mark.parametrize("servers", [(), ("apply",)], ids=["unplaced", "placed"])
def test_opencodes_apply_patch_is_reported_to_a_hook_as_fs_write(servers, tmp_path, monkeypatch):
    # The every-"_" split also reads apply_patch as "@apply/patch". The hook's
    # stdin takes the FIRST name, so a "*" deny that branches on tool_name ==
    # "fs_write" must be told fs_write, or every apply_patch write goes through.
    tool_id = _qualified("opencode", "apply_patch", servers)
    names = spec_hooks.spec_hook_tool_names(tool_id)
    assert names is not None and names[0] == "fs_write"
    assert {"@apply/patch", "mcp__apply__patch"} <= set(names)
    event = SimpleNamespace(
        title="apply_patch",
        tool_input=None,
        tool_name="",
        mcp_server_name="",
        harness_tool_id=tool_id,
    )
    spec_hooks._cache.clear()
    hooks, _ = spec_hooks._convert(
        "a1", {"hooks": {"preToolUse": [{"matcher": "*", "command": "d"}]}}
    )
    ran = _fake_runs(monkeypatch, exit_code=0)
    asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path),
            list(hooks),
            None,
            event.title,
            event.tool_input,
            tool_identity=event.tool_name,
            mcp_server=event.mcp_server_name,
            harness_tool_id=event.harness_tool_id,
        )
    )
    assert [t for _, _, t, _ in ran] == ["fs_write"]


def test_an_mcp_name_that_is_no_native_tool_keeps_its_split_first():
    names = spec_hooks.spec_hook_tool_names(
        _qualified("opencode", "kirocrew-core_monitor_start", ("kirocrew-core",))
    )
    assert names is not None and names[0] == "@kirocrew-core/monitor_start"


def test_an_ambiguous_fused_name_keeps_every_split(tmp_path, monkeypatch):
    # Servers "a" and "a_b" both fit "a_b_c": a deny on either one still runs.
    names = spec_hooks.spec_hook_tool_names(_qualified("opencode", "a_b_c", ("a", "a_b")))
    assert names is not None
    assert {"@a_b/c", "@a/b_c", "mcp__a_b__c", "mcp__a__b_c", "a_b_c"} <= set(names)


def test_a_name_with_no_underscore_stays_as_written():
    assert _qualified("opencode", "bash", ("kirocrew-core",)) == "opencode#bash"
    assert _qualified("opencode", "webfetch", ()) == "opencode#webfetch"
    # goose states its server itself, so it is never split.
    assert _qualified("goose", "kirocrew-core_x", ("kirocrew-core",)) == "goose#kirocrew-core_x"


def _qualified(backend, name, servers):
    from kiro_crew.acp.harness_tool_names import qualified_harness_tool_id

    return qualified_harness_tool_id(backend, name, servers)


def test_a_goose_mcp_deny_hook_meets_the_served_tool(tmp_path, monkeypatch):
    caches: dict = {
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "harness_tool_name_cache": {},
    }
    event = None
    for line in (_FRAMES / "goose/mcp-stdio-mount-live.jsonl").read_text().splitlines():
        frame = json.loads(line) if line.strip() else {}
        params = frame.get("params") or {}
        if frame.get("method") == "session/update":
            _dispatch.parse_session_update(params.get("update") or {}, **caches)
        elif frame.get("method") == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=params)
            event, _ = _dispatch.build_permission_event(
                msg, **caches, harness_backend=ACP_BACKEND_GOOSE
            )
            break
    assert event is not None
    assert _blocks(event, "mcp__crew-probe__crew_probe_echo", tmp_path, monkeypatch)
    assert not _blocks(event, "mcp__other__*", tmp_path, monkeypatch)


def test_the_direct_client_splits_by_the_servers_it_placed():
    client = _bare_client(ACP_BACKEND_OPENCODE)
    client._session_mcp_cache = [{"name": "kirocrew-core"}, {"name": 3}]
    assert client._placed_mcp_server_names() == ("kirocrew-core",)
    del client._session_mcp_cache
    assert client._placed_mcp_server_names() == ()


def test_the_direct_client_permission_event_carries_the_mcp_split():
    # End to end on AcpClient: the live opencode MCP tool_call, then a permission
    # request for it, with kirocrew-core placed on the session's array.
    client = _bare_client(ACP_BACKEND_OPENCODE)
    client._session_mcp_cache = [{"name": "kirocrew-core"}]
    call_id = ""
    for line in (_FRAMES / "opencode/mcp-directive-call-live.jsonl").read_text().splitlines():
        frame = json.loads(line) if line.strip() else {}
        update = (frame.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") == "tool_call":
            call_id = update["toolCallId"]
            client._extract_tool_event(
                JsonRpcMessage(method="session/update", params=frame["params"])
            )
            break
    assert call_id
    tool_call = {"toolCallId": call_id, "title": "x", "kind": "other", "rawInput": {}}
    options = [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}]
    msg = JsonRpcMessage(
        id=9,
        method="session/request_permission",
        params={"sessionId": "s", "toolCall": tool_call, "options": options},
    )
    event = client._build_permission_event(msg)
    assert event is not None
    assert event.harness_tool_id.split("|")[0] == "opencode#@kirocrew-core/monitor_start"


def test_the_direct_client_splits_a_rewritten_server_by_its_placed_name():
    # "docs.server" reaches the title as "docs_server_lookup". Only the placed
    # name splits it as @docs.server/lookup; the every-"_" split alone would
    # start with @docs/server_lookup, so this pins the client passing its
    # placed servers to the permission event.
    client = _bare_client(ACP_BACKEND_OPENCODE)
    client._session_mcp_cache = [{"name": "docs.server"}]
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "c9",
        "title": "docs_server_lookup",
        "kind": "other",
    }
    client._extract_tool_event(
        JsonRpcMessage(method="session/update", params={"sessionId": "s", "update": update})
    )
    tool_call = {"toolCallId": "c9", "title": "x", "kind": "other", "rawInput": {}}
    options = [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}]
    msg = JsonRpcMessage(
        id=9,
        method="session/request_permission",
        params={"sessionId": "s", "toolCall": tool_call, "options": options},
    )
    event = client._build_permission_event(msg)
    assert event is not None
    assert event.harness_tool_id.split("|")[0] == "opencode#@docs.server/lookup"


def _direct_permission_event(client, title: str):
    """A ``tool_call`` titled *title*, then its permission request, on *client*."""
    update = {"sessionUpdate": "tool_call", "toolCallId": "c8", "title": title, "kind": "other"}
    client._extract_tool_event(
        JsonRpcMessage(method="session/update", params={"sessionId": "s", "update": update})
    )
    tool_call = {"toolCallId": "c8", "title": "x", "kind": "other", "rawInput": {}}
    options = [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}]
    return client._build_permission_event(
        JsonRpcMessage(
            id=8,
            method="session/request_permission",
            params={"sessionId": "s", "toolCall": tool_call, "options": options},
        )
    )


def test_a_rewritten_server_from_opencodes_own_config_meets_its_exact_deny(tmp_path, monkeypatch):
    # Crew placed nothing; opencode's own config mounts "docs.server", which the
    # spawn's read-back named. The fused "docs_server_lookup" splits back to the
    # exact name, so a deny written mcp__docs.server__lookup still blocks.
    client = _bare_client(ACP_BACKEND_OPENCODE)
    client._opencode_config_mcp_servers = ("docs.server",)
    event = _direct_permission_event(client, "docs_server_lookup")
    assert event is not None
    assert event.harness_tool_id.split("|")[0] == "opencode#@docs.server/lookup"
    assert _blocks(event, "mcp__docs.server__lookup", tmp_path, monkeypatch)
    assert _blocks(event, "mcp__docs.server__*", tmp_path, monkeypatch)


def test_a_long_mcp_title_keeps_its_identity_and_meets_its_deny(tmp_path, monkeypatch):
    # KAS's 128-character id bound does not apply: a 130-character server name
    # fused with its tool is still read, so a deny written for it still blocks.
    from kiro_crew.acp.harness_tool_names import MAX_HARNESS_TOOL_NAME_LEN

    server = "s" * 130
    client = _bare_client(ACP_BACKEND_OPENCODE)
    client._session_mcp_cache = [{"name": server}]
    event = _direct_permission_event(client, f"{server}_lookup")
    assert event is not None
    assert event.harness_tool_id.split("|")[0] == f"opencode#@{server}/lookup"
    assert _blocks(event, f"mcp__{server}__*", tmp_path, monkeypatch)
    # Past the bound the name is dropped, not cut.
    assert _dispatch.harness_tool_name({"title": "a" * (MAX_HARNESS_TOOL_NAME_LEN + 1)}) == ""


def test_the_every_underscore_split_of_a_bounded_name_stays_small():
    # The worst title the bound admits ("a_a_..._a") splits at every "_"; the id
    # it yields stays within a fixed size, not megabytes.
    from kiro_crew.acp.harness_tool_names import MAX_HARNESS_TOOL_NAME_LEN

    title = ("a_" * MAX_HARNESS_TOOL_NAME_LEN)[: MAX_HARNESS_TOOL_NAME_LEN - 1] + "a"
    assert _dispatch.harness_tool_name({"title": title}) == title
    tool_id = _qualified("opencode", title, ())
    assert tool_id.count("|") < MAX_HARNESS_TOOL_NAME_LEN
    assert len(tool_id) < MAX_HARNESS_TOOL_NAME_LEN * (MAX_HARNESS_TOOL_NAME_LEN + 2)


@pytest.mark.parametrize("direct", [False, True], ids=["shared", "direct"])
def test_a_reused_call_id_does_not_keep_the_old_tool_name(direct):
    # A second tool_call under the same id with no usable name must not inherit
    # the first call's name, or a hook for that tool would meet the wrong call.
    first = {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "bash", "kind": "execute"}
    second = {**first, "title": "shell · echo hi"}
    if direct:
        client = _bare_client(ACP_BACKEND_OPENCODE)
        for update in (first, second):
            client._extract_tool_event(
                JsonRpcMessage(method="session/update", params={"sessionId": "s", "update": update})
            )
        assert client._tool_call_harness_tool_name["c1"] == ""
    else:
        names: dict = {}
        for update in (first, second):
            _dispatch.parse_session_update(update, harness_tool_name_cache=names)
        assert names == {"c1": ""}


# ── a project-scoped agent spec: the one the session runs ──


def _project_spec(root: Path, name: str, spec: dict | str) -> Path:
    d = root / ".kiro" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    body = spec if isinstance(spec, str) else json.dumps({"name": name, "prompt": "p", **spec})
    (d / f"{name}.json").write_text(body, encoding="utf-8")
    return root


@pytest.mark.parametrize("backend", _MIRRORS)
def test_an_untrusted_project_spec_hook_is_not_loaded_on_the_mirror(backend, agents_dir, tmp_path):
    # The checkout's copy of the agent carries a hook; a hook is a command, and the
    # checkout is not trusted to choose commands, so neither turn loop loads it.
    _write_spec(agents_dir, "a1", {})
    project = _project_spec(
        tmp_path / "proj",
        "a1",
        {"hooks": {"preToolUse": [{"matcher": "execute_bash", "command": "deny.sh"}]}},
    )
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend, str(project)), "a1"))
    assert not turn.unreadable and turn.gated and turn.hooks == []
    hooks, unreadable, _ = asyncio.run(
        chat_runner._prepare_spec_hooks(
            None, None, _provider(backend, str(project)), "a1", is_new=False
        )
    )
    assert not unreadable and hooks == []


@pytest.mark.parametrize("backend", _MIRRORS)
def test_a_user_spec_deny_hook_still_blocks_under_a_shadowing_project_spec(
    backend, agents_dir, tmp_path, monkeypatch
):
    # The user wrote the deny; the checkout ships a same-name spec with its own hook.
    _write_spec(
        agents_dir,
        "a1",
        {"preToolUse": [{"matcher": "execute_bash", "command": "user-deny.sh"}]},
    )
    project = _project_spec(
        tmp_path / "proj",
        "a1",
        {"hooks": {"preToolUse": [{"matcher": "execute_bash", "command": "repo.sh"}]}},
    )
    ran = _fake_runs(monkeypatch, exit_code=2)
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend, str(project)), "a1"))
    assert not turn.unreadable and [h.command for h in turn.hooks] == ["user-deny.sh"]
    event = _permission_event(backend)
    reason = asyncio.run(
        hooks_mod.permission_pre_tool_block(
            ScriptHookStore(config_dir=tmp_path),
            turn.hooks,
            turn.cwd,
            event.title,
            event.tool_input,
            tool_identity=event.tool_name,
            mcp_server=event.mcp_server_name,
            harness_tool_id=event.harness_tool_id,
        )
    )
    assert [c for _, c, _, _ in ran] == ["user-deny.sh"] and reason is not None
    hooks, unreadable, _ = asyncio.run(
        chat_runner._prepare_spec_hooks(
            None, None, _provider(backend, str(project)), "a1", is_new=False
        )
    )
    assert not unreadable and [h.command for h in hooks] == ["user-deny.sh"]


@pytest.mark.parametrize("backend", _MIRRORS)
def test_an_unreadable_project_spec_fails_closed(backend, agents_dir, tmp_path):
    _write_spec(agents_dir, "a1", {})
    project = _project_spec(tmp_path / "proj", "a1", "{not json")
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend, str(project)), "a1"))
    assert turn.unreadable and turn.gated


def test_kas_still_reads_the_user_level_alone(agents_dir, tmp_path):
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    _write_spec(agents_dir, "a1", {})
    project = _project_spec(
        tmp_path / "proj", "a1", {"hooks": {"preToolUse": [{"command": "deny.sh"}]}}
    )
    assert spec_hooks.spec_project_dir(_provider(ACP_BACKEND_KAS, str(project))) is None
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(ACP_BACKEND_KAS, str(project)), "a1"))
    assert turn.hooks == []


@pytest.mark.parametrize("matcher", ["mcp__docs.server__lookup", "docs_server_lookup"])
def test_a_server_name_opencode_rewrites_still_meets_its_deny_hook(matcher, tmp_path, monkeypatch):
    # opencode writes "docs.server" as "docs_server" in the title; a deny written
    # with the placed name or with the wire spelling still blocks the call.
    tool_id = _qualified("opencode", "docs_server_lookup", ("docs.server",))
    assert tool_id.split("|")[0] == "opencode#@docs.server/lookup"
    event = SimpleNamespace(
        title="docs_server_lookup",
        tool_input=None,
        tool_name="",
        mcp_server_name="",
        harness_tool_id=tool_id,
    )
    assert _blocks(event, matcher, tmp_path, monkeypatch)


@pytest.mark.parametrize("matcher", ["mcp__local-chorus-mcp__search", "mcp__local-chorus-mcp__*"])
def test_a_server_from_opencodes_own_config_still_meets_its_deny_hook(
    matcher, tmp_path, monkeypatch
):
    # Crew placed only kirocrew-core; opencode's own config mounted local-chorus-mcp.
    tool_id = _qualified("opencode", "local-chorus-mcp_search", ("kirocrew-core",))
    event = SimpleNamespace(
        title="local-chorus-mcp_search",
        tool_input=None,
        tool_name="",
        mcp_server_name="",
        harness_tool_id=tool_id,
    )
    assert _blocks(event, matcher, tmp_path, monkeypatch)
    assert not _blocks(event, "mcp__kirocrew-core__*", tmp_path, monkeypatch)
