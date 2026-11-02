"""An agent spec's ``hooks`` on KAS, run through Crew's real turn loop.

kiro-cli runs a spec's ``hooks`` itself; KAS's agent schema has no slot for them,
so Crew's turn loop fires them (``agent_sdk.spec_hooks``). These tests drive one
real ``chat_runner._run_chat`` turn on a KAS session, with the real hook store and
the spec on disk, and check that each event kiro-cli runs a hook for runs the
spec's hook here too, with the tool matcher read the way kiro-cli reads it: in
kiro-cli's tool names, not in KAS's call titles.

Only the hook process itself is stubbed (``run_script_hook``): what is under test
is which hooks the turn loop chose to fire.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from chat_runner_coverage_helpers import (
    _answer_card,
    _complete,
    _drive,
    _runner_state,
    _set_stream,
    _slot,
)

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import ScriptHookResult, ScriptHookStore
from kiro_crew.platform.context import redact_via_context
from kiro_crew.providers.base import EVENT_TOOL_CALL_UPDATE, LLMEvent

_AGENT = "kas-hooked"

#: The ``tool_response.output`` each spec hook was handed, in run order.
_OUTPUTS: list = []
#: The ``tool_input`` each spec hook was handed (``None`` when absent), in run order.
_INPUTS: list = []


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def fired(tmp_path, monkeypatch) -> list[tuple[str, str]]:
    """``(event, hook name)`` for every spec hook the turn loop runs."""
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    seen: list[tuple[str, str]] = []
    _OUTPUTS.clear()
    _INPUTS.clear()

    async def run(hook, context="", hook_event=None, **_kwargs):
        if hook.id.startswith("spec:"):
            seen.append((hook.event, hook.command))
            _OUTPUTS.append(((hook_event or {}).get("tool_response") or {}).get("output"))
            _INPUTS.append((hook_event or {}).get("tool_input"))
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", run)
    return seen


def _write_spec(tmp_path, hooks) -> None:
    """The spec where both the turn's agent admission and the hook reader find it."""
    from kiro_crew.agent_discovery import clear_list_agents_cache
    from kiro_crew.config.loader import refresh_materialized_agents

    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    spec = {"name": _AGENT, "prompt": "p", "tools": ["*"], "hooks": hooks}
    (agents / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")
    clear_list_agents_cache()
    refresh_materialized_agents()


def _kas_turn(tmp_path, *, is_new: bool):
    state, client = _runner_state(tmp_path, hook_store=ScriptHookStore(tmp_path / "hooks"))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    client.cwd = str(tmp_path)
    client.session_id = "sess_kas_1"
    client.kas_projected_agent = _AGENT
    # The projection withheld every auto-approval the hooks cover, so the
    # session is fresh: nothing it auto-approves meets a PreToolUse hook.
    client.kas_auto_approved_capabilities = frozenset()
    client.approve_tool = MagicMock(side_effect=lambda *a, **k: _noop())
    state.sessions.get_or_create.return_value = (client, is_new, False)
    slot = _slot()
    slot.agent = _AGENT
    return state, client, slot


async def _noop():
    return None


def _shell_call(*, with_request: bool) -> list[LLMEvent]:
    """KAS's frames for one shell call: no tool id on the call, ``run_command``
    on the permission request (when there is one), a prose title throughout."""
    events = [
        LLMEvent(
            kind=EVENT_TOOL_CALL,
            title="List the files",
            tool_call_id="tc-1",
            tool_kind="execute",
            is_shell=True,
        )
    ]
    if with_request:
        events.append(
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="List the files",
                tool_kind="execute",
                tool_input=json.dumps({"command": "ls"}),
                request_id="req-cov-1",
                tool_call_id="tc-1",
                is_shell=True,
                harness_tool_id="run_command",
            )
        )
    events += [
        LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-1",
            tool_output="a.txt",
            tool_final=True,
            tool_kind="execute",
        ),
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
        _complete(),
    ]
    return events


_EVERY_EVENT = {
    "agentSpawn": [{"command": "spawn"}],
    "userPromptSubmit": [{"command": "prompt"}],
    "preToolUse": [{"matcher": "execute_bash", "command": "pre"}],
    "postToolUse": [{"matcher": "execute_bash", "command": "post"}],
    "stop": [{"command": "stop"}],
}


@pytest.mark.asyncio
async def test_a_new_kas_session_runs_every_spec_hook_event(tmp_path, fired):
    _write_spec(tmp_path, _EVERY_EVENT)
    state, client, slot = _kas_turn(tmp_path, is_new=True)
    _set_stream(client, _shell_call(with_request=True))
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "approved")
    await _drive(state, slot)
    assert sorted(cmd for _, cmd in fired) == ["post", "pre", "prompt", "spawn", "stop"]
    # Each once: the tool-call frame does not fire PreToolUse a second time.
    assert len(fired) == 5


@pytest.mark.asyncio
async def test_agent_spawn_runs_only_on_the_sessions_first_turn(tmp_path, fired):
    _write_spec(tmp_path, {"agentSpawn": [{"command": "spawn"}], "stop": [{"command": "stop"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
    await _drive(state, slot)
    assert [cmd for _, cmd in fired] == ["stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("matcher", "runs"),
    [
        ("execute_bash", True),
        ("shell", True),
        ("execute_*", True),
        ("*", True),
        ("fs_write", False),
        ("@git/status", False),
    ],
)
async def test_a_post_tool_use_matcher_reads_kiro_cli_tool_names(tmp_path, fired, matcher, runs):
    """kiro-cli matches a postToolUse hook against its own tool name. KAS's call
    frame names no tool and titles the call in prose, so the turn loop has to
    match on what it knows of the call: the id the permission request stated."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": matcher, "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    _set_stream(client, _shell_call(with_request=True))
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "approved")
    await _drive(state, slot)
    assert ([cmd for _, cmd in fired] == ["post"]) is runs


@pytest.mark.asyncio
@pytest.mark.parametrize(("matcher", "runs"), [("fs_write", True), ("execute_bash", False)])
async def test_a_post_tool_use_matcher_meets_an_edit_its_request_named(
    tmp_path, fired, matcher, runs
):
    """Not a shell call, so only the permission request's id names the edit."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": matcher, "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    _set_stream(
        client,
        [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Edit notes",
                tool_call_id="tc-1",
                tool_kind="edit",
            ),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="Edit notes",
                tool_kind="edit",
                tool_input=json.dumps({"path": "notes.md"}),
                request_id="req-cov-1",
                tool_call_id="tc-1",
                harness_tool_id="str_replace",
            ),
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-1",
                tool_output="ok",
                tool_final=True,
                tool_kind="edit",
            ),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            _complete(),
        ],
    )
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "approved")
    await _drive(state, slot)
    assert ([cmd for _, cmd in fired] == ["post"]) is runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("matcher", "runs"),
    [("@probefs/read_file", True), ("mcp__probefs__read_file", True), ("@other/*", False)],
)
async def test_a_post_tool_use_matcher_meets_an_mcp_call_by_its_server_form(
    tmp_path, fired, matcher, runs
):
    """The request's harness id alone names no MCP form, so the call's trusted
    server and tool are kept beside it, as PreToolUse matches them."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": matcher, "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    identity = {"tool_name": "read_file", "mcp_server_name": "probefs"}
    _set_stream(
        client,
        [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="@probefs/read_file", tool_call_id="tc-1", **identity
            ),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="@probefs/read_file",
                tool_input=json.dumps({"path": "notes.md"}),
                request_id="req-cov-1",
                tool_call_id="tc-1",
                harness_tool_id="mcp_probefs_read_file",
                **identity,
            ),
            LLMEvent(
                kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_output="ok", tool_final=True
            ),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            _complete(),
        ],
    )
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "approved")
    await _drive(state, slot)
    assert ([cmd for _, cmd in fired] == ["post"]) is runs


@pytest.mark.asyncio
async def test_post_tool_use_runs_once_on_the_terminal_result(tmp_path, fired):
    """A progress frame carrying partial output is not the finished call: the hook
    runs once, on the terminal result, still meeting the call by its id."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": "execute_bash", "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    events = _shell_call(with_request=True)
    progress = LLMEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id="tc-1",
        tool_output="a.t",
        tool_status="in_progress",
        tool_kind="execute",
    )
    events.insert(2, progress)
    _set_stream(client, events)
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "approved")
    await _drive(state, slot)
    assert [cmd for _, cmd in fired] == ["post"]
    assert _OUTPUTS == ["a.txt"]


@pytest.mark.asyncio
async def test_a_post_tool_use_matcher_meets_an_auto_approved_shell_call(tmp_path, fired):
    """No permission request at all: the call's shell classification is what
    names it, as ``execute_bash`` is the only tool kiro-cli runs a command with."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": "execute_bash", "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    _set_stream(client, _shell_call(with_request=False))
    await _drive(state, slot)
    assert [cmd for _, cmd in fired] == ["post"]


@pytest.mark.asyncio
async def test_post_tool_use_gets_the_tool_input_its_request_carried(tmp_path, fired):
    """A hook that acts on the file a call touched needs the call's arguments:
    PostToolUse is handed the ``tool_input`` its PreToolUse sibling got."""
    _write_spec(
        tmp_path,
        {
            "preToolUse": [{"matcher": "fs_write", "command": "pre"}],
            "postToolUse": [{"matcher": "fs_write", "command": "post"}],
        },
    )
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    _set_stream(
        client,
        [
            LLMEvent(kind=EVENT_TOOL_CALL, title="Write notes", tool_call_id="tc-1"),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="Write notes",
                tool_input=json.dumps({"command": "create", "path": "notes.md"}),
                request_id="req-cov-1",
                tool_call_id="tc-1",
                harness_tool_id="fs_write",
            ),
            LLMEvent(
                kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_output="ok", tool_final=True
            ),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            _complete(),
        ],
    )
    state.push_slots_update.side_effect = lambda: _answer_card(slot, "approved")
    await _drive(state, slot)
    assert [cmd for _, cmd in fired] == ["pre", "post"]
    assert _INPUTS == [{"command": "create", "path": "notes.md"}] * 2


@pytest.mark.asyncio
async def test_post_tool_use_gets_the_tool_input_of_an_auto_approved_call(tmp_path, fired):
    """No permission request: the arguments come from the call's own frame."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": "execute_bash", "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    events = _shell_call(with_request=False)
    events[0] = LLMEvent(
        kind=EVENT_TOOL_CALL,
        title="List the files",
        tool_call_id="tc-1",
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": "ls"}),
    )
    _set_stream(client, events)
    await _drive(state, slot)
    assert [cmd for _, cmd in fired] == ["post"]
    assert _INPUTS == [{"command": "ls"}]


@pytest.mark.asyncio
async def test_post_tool_use_gets_the_tool_input_a_call_update_supplied(tmp_path, fired):
    """A backend whose call frame streams empty arguments sends them on a
    later ``tool_call_update``; PostToolUse still gets them."""
    _write_spec(tmp_path, {"postToolUse": [{"matcher": "execute_bash", "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    events = _shell_call(with_request=False)
    events.insert(
        1,
        LLMEvent(
            kind=EVENT_TOOL_CALL_UPDATE,
            title="List the files",
            tool_call_id="tc-1",
            tool_kind="execute",
            is_shell=True,
            tool_input=json.dumps({"command": "ls -la"}),
        ),
    )
    _set_stream(client, events)
    await _drive(state, slot)
    assert [cmd for _, cmd in fired] == ["post"]
    assert _INPUTS == [{"command": "ls -la"}]


@pytest.mark.asyncio
async def test_post_tool_use_has_no_tool_input_when_the_call_carried_none(tmp_path, fired):
    _write_spec(tmp_path, {"postToolUse": [{"matcher": "execute_bash", "command": "post"}]})
    state, client, slot = _kas_turn(tmp_path, is_new=False)
    _set_stream(client, _shell_call(with_request=False))
    await _drive(state, slot)
    assert _INPUTS == [None]


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ('{"path": "a.md"}', {"path": "a.md"}),
        ("", None),
        ("not json", None),
        ('["a.md"]', None),
        (None, None),
    ],
)
def test_hook_tool_input_parses_only_a_json_object(raw, parsed):
    assert spec_hooks.hook_tool_input(raw) == parsed


def test_an_edits_tool_input_comes_from_its_redacted_arguments():
    """An edit's ``tool_input`` is the diff drawn for its approval card, so the
    hook gets the arguments it was drawn from, with credentials redacted."""
    from types import SimpleNamespace

    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    store: dict = {}
    spec_hooks.remember_tool_input(
        store,
        SimpleNamespace(
            tool_call_id="tc-1",
            tool_input="--- a/notes.md\n+++ b/notes.md\n@@ -0,0 +1 @@\n+hi\n",
            raw_tool_params={"command": "create", "path": "notes.md", "file_text": secret},
        ),
        scrub=redact_via_context,
    )
    assert store["tc-1"]["path"] == "notes.md"
    assert secret not in json.dumps(store["tc-1"])


def test_a_frame_without_arguments_keeps_what_the_other_frame_recorded():
    from types import SimpleNamespace

    store: dict = {}
    spec_hooks.remember_tool_input(
        store,
        SimpleNamespace(tool_call_id="tc-1", tool_input='{"path": "a.md"}'),
        scrub=redact_via_context,
    )
    spec_hooks.remember_tool_input(
        store, SimpleNamespace(tool_call_id="tc-1", tool_input=""), scrub=redact_via_context
    )
    assert store == {"tc-1": {"path": "a.md"}}
