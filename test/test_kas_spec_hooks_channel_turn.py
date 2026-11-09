"""An agent spec's ``hooks`` and the Hooks page's tool hooks on a channel turn.

A channel turn (Slack, Discord, Telegram, ...) runs through the shared
``TurnDriver``, not the dashboard's turn loop. KAS's agent schema has no slot for
a spec's ``hooks``, so Crew fires them itself, and the driver has to do that the
way the dashboard, subagent and task-runner loops do: a PreToolUse hook gates the
permission request before any approval (a spec ``allowedTools`` grant included),
a PostToolUse hook runs on the finished call, and an AgentSpawn hook runs on a new
session. The Hooks page's PreToolUse hooks gate every backend's calls.

These tests drive a real ``TurnDriver.run`` with the real hook store and the spec
on disk. Only the hook process itself is stubbed (``run_script_hook``): what is
under test is which hooks the driver chose to fire, and what it did with the
verdict.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew import name_grant
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.constants import DENY_CAUSE_POLICY
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookResult,
    ScriptHookStore,
)
from kiro_crew.messaging import APPROVAL_INTERACTIVE, TransportCapabilities, TurnDriver
from kiro_crew.messaging import driver as driver_mod
from kiro_crew.messaging.renderer import SilentRenderer
from kiro_crew.messaging.script_hooks import ScriptHookTurn
from kiro_crew.providers.base import LLMEvent

_AGENT = "kas-channel-hooked"
_SESSION = "slack:dm:U1"

_SPEC_HOOKS = {
    "agentSpawn": [{"command": "spawn"}],
    "preToolUse": [{"matcher": "execute_bash", "command": "guard"}],
    "postToolUse": [{"matcher": "execute_bash", "command": "post"}],
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


class _Runs:
    """Every hook the store ran, and the exit code each command answers with."""

    def __init__(self) -> None:
        self.exits: dict[str, int] = {}
        self.stdout: dict[str, str] = {}
        #: ``(event, command, from the spec, hook_event)`` in run order.
        self.seen: list[tuple[str, str, bool, dict]] = []

    def commands(self, *, spec: bool) -> list[tuple[str, str]]:
        return [(e, c) for e, c, from_spec, _ in self.seen if from_spec is spec]


@pytest.fixture
def runs(tmp_path, monkeypatch) -> _Runs:
    record = _Runs()
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)

    async def run(hook, context="", hook_event=None, **_kwargs):
        record.seen.append(
            (hook.event, hook.command, hook.id.startswith("spec:"), dict(hook_event or {}))
        )
        code = record.exits.get(hook.command, 0)
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=code,
            stdout=record.stdout.get(hook.command, ""),
            stderr="denied by the guard" if code == 2 else "",
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", run)
    # The user's standalone ~/.kiro/hooks files are not part of this turn.
    monkeypatch.setattr(spec_hooks, "_user_hooks_dir", lambda: None)
    return record


@pytest.fixture
def store(tmp_path, monkeypatch) -> ScriptHookStore:
    """The Hooks page's store, registered as the gateway registers it."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    s = ScriptHookStore(hooks_dir)
    monkeypatch.setattr(hooks_mod, "_global_script_hook_store", s)
    return s


@pytest.fixture
def wire(monkeypatch) -> SimpleNamespace:
    """What the driver audited and steered, and an approve-by-name gate."""
    seen = SimpleNamespace(audit=[], steers=[])

    async def _no_refusal(_event):
        return None

    async def _steer(_provider, _title, reason, *, cause, bound_secs):
        seen.steers.append((reason, cause))
        return ""

    class _Sel:
        def log_api_access(self, **kw):
            seen.audit.append(kw.get("resources", ""))

    monkeypatch.setattr(name_grant, "refusal_for_event", _no_refusal)
    monkeypatch.setattr(driver_mod, "steer_refusal_notice", _steer)
    monkeypatch.setattr(driver_mod, "sel", lambda: _Sel())
    return seen


#: The operator's own ``~/.kiro/hooks`` file: one hook on each event, each command
#: named ``file-<event>``.
_USER_FILE_HOOKS = [
    {"name": name, "trigger": trigger, "action": {"type": "command", "command": f"file-{name}"}}
    for name, trigger in (
        ("spawn", "AgentSpawn"),
        ("prompt", "UserPromptSubmit"),
        ("guard", "PreToolUse"),
        ("post", "PostToolUse"),
        ("stop", "Stop"),
    )
]


def _write_user_hook_file(tmp_path, monkeypatch) -> None:
    hooks_dir = tmp_path / "user-hooks"
    hooks_dir.mkdir()
    (hooks_dir / "mine.json").write_text(
        json.dumps({"version": "v1", "hooks": _USER_FILE_HOOKS}), encoding="utf-8"
    )
    monkeypatch.setattr(spec_hooks, "_user_hooks_dir", lambda: hooks_dir)
    spec_hooks._user_file_cache.clear()


def _file_runs(runs: _Runs) -> list[tuple[str, str]]:
    return [(e, c) for e, c, _from_spec, _ in runs.seen if c.startswith("file-")]


def _write_spec(hooks) -> None:
    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    spec = {"name": _AGENT, "prompt": "p", "tools": ["*"], "hooks": hooks}
    (agents / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _shell_call(*, finished: bool) -> list[LLMEvent]:
    """KAS's frames for one shell call: no tool id on the call, ``run_command`` on
    its permission request, a prose title throughout."""
    events = [
        LLMEvent(
            kind=EVENT_TOOL_CALL,
            title="Touch a file",
            tool_call_id="t1",
            tool_kind="execute",
            is_shell=True,
        ),
        LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Touch a file",
            tool_kind="execute",
            tool_input=json.dumps({"command": "touch /tmp/hooktest-ran"}),
            request_id="r1",
            tool_call_id="t1",
            is_shell=True,
            harness_tool_id="run_command",
        ),
    ]
    if finished:
        events.append(
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="t1",
                tool_output="ran",
                tool_final=True,
                tool_status="completed",
                tool_kind="execute",
            )
        )
    events += [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    return events


class _Provider:
    def __init__(self, events, tmp_path, *, backend: str = ACP_BACKEND_KAS) -> None:
        self.capabilities = capabilities_for(backend)
        self.cwd = str(tmp_path)
        self.kas_projected_agent = _AGENT
        self.events = events
        self.messages: list[str] = []
        self.approved: list[str] = []
        self.rejected: list[str] = []

    async def stream(self, message):
        self.messages.append(message)
        for event in self.events:
            yield event

    async def approve_tool(self, request_id, **_kw):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


def _driver(provider, *, operator: bool = True, **kwargs) -> TurnDriver:
    """A Slack DM turn whose gate grants the call by the spec's ``allowedTools``."""
    return TurnDriver(
        provider,
        SilentRenderer(TransportCapabilities()),
        approval_mode=APPROVAL_INTERACTIVE,
        tool_gate=lambda _event: "auto_approve",
        audit_session_key=_SESSION,
        audit_agent=_AGENT,
        script_hooks=ScriptHookTurn(_SESSION, "run touch") if operator else None,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_a_spec_pre_tool_use_deny_refuses_a_granted_call(tmp_path, runs, store, wire):
    """The repro: an ``allowedTools`` grant must not run past the spec's exit-2 guard."""
    _write_spec(_SPEC_HOOKS)
    runs.exits["guard"] = 2
    provider = _Provider(_shell_call(finished=False), tmp_path)
    await _driver(provider).run("run touch")
    assert runs.commands(spec=True) == [(HOOK_EVENT_PRE_TOOL_USE, "guard")]
    assert provider.approved == []
    assert provider.rejected == ["r1"]
    assert [cause for _reason, cause in wire.steers] == [DENY_CAUSE_POLICY]
    assert "denied by the guard" in wire.steers[0][0]
    # Audited before the steer and the reject, under its own reason.
    assert any("reason=pre_tool_hook" in row for row in wire.audit)


@pytest.mark.asyncio
async def test_an_allowed_call_runs_and_its_post_tool_use_meets_execute_bash(
    tmp_path, runs, store, wire
):
    _write_spec(_SPEC_HOOKS)
    provider = _Provider(_shell_call(finished=True), tmp_path)
    await _driver(provider).run("run touch")
    assert provider.approved == ["r1"]
    assert provider.rejected == []
    assert runs.commands(spec=True) == [
        (HOOK_EVENT_PRE_TOOL_USE, "guard"),
        (HOOK_EVENT_POST_TOOL_USE, "post"),
    ]
    post_event = runs.seen[-1][3]
    # The `execute_bash` matcher met the call; its stdin names it as kiro-cli's
    # V2 engine does (`spec_hooks.kiro_cli_hook_payload`).
    assert post_event["tool_name"] == "shell"
    assert post_event["tool_response"] == {"output": "ran"}
    assert post_event["session_key"] == _SESSION


@pytest.mark.asyncio
async def test_every_spec_hook_is_handed_the_acp_session_id(tmp_path, runs, store, wire):
    """A spec guard that reads ``session_id`` from stdin, as kiro-cli hands it, must
    get it on a channel turn too: missing, the guard crashes and refuses nothing."""
    _write_spec(
        {
            **_SPEC_HOOKS,
            "userPromptSubmit": [{"command": "prompt"}],
            "stop": [{"command": "stop"}],
        }
    )
    provider = _Provider(_shell_call(finished=True), tmp_path)
    provider.session_id = "acp-sess-7"
    await _driver(provider, is_new_session=True).run("run touch")
    fired = [(e, c, ev) for e, c, from_spec, ev in runs.seen if from_spec]
    assert [(e, c) for e, c, _ in fired] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "prompt"),
        (HOOK_EVENT_PRE_TOOL_USE, "guard"),
        (HOOK_EVENT_POST_TOOL_USE, "post"),
        (HOOK_EVENT_STOP, "stop"),
    ]
    assert [ev.get("session_id") for _, _, ev in fired] == ["acp-sess-7"] * 5


@pytest.mark.asyncio
async def test_a_hooks_page_post_tool_use_matcher_meets_the_calls_canonical_name(
    tmp_path, runs, store, wire
):
    """A Hooks-page PostToolUse hook scoped to ``execute_bash`` runs on a KAS shell
    call whose title is prose, as it does on the dashboard, spec hooks or none."""
    _write_spec({})
    store.create(
        {
            "name": "store-post",
            "event": HOOK_EVENT_POST_TOOL_USE,
            "matcher": "execute_bash",
            "command": "watch",
        }
    )
    provider = _Provider(_shell_call(finished=True), tmp_path)
    await _driver(provider).run("run touch")
    assert provider.approved == ["r1"]
    assert runs.commands(spec=False) == [(HOOK_EVENT_POST_TOOL_USE, "watch")]


@pytest.mark.asyncio
async def test_an_ungated_calls_hooks_page_pre_tool_use_meets_its_canonical_name(
    tmp_path, runs, store, wire
):
    """On claude a pre-allowed Bash call raises no permission request, so the
    informational PreToolUse is its only one: a Hooks-page hook scoped to
    ``execute_bash`` must meet the call titled ``Terminal`` there."""
    _write_spec({})
    store.create(
        {
            "name": "store-pre",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "matcher": "execute_bash",
            "command": "audit",
        }
    )
    events = [
        LLMEvent(
            kind=EVENT_TOOL_CALL,
            title="Terminal",
            tool_call_id="t1",
            tool_kind="execute",
            is_shell=True,
        ),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    provider = _Provider(events, tmp_path, backend=ACP_BACKEND_CLAUDE)
    await _driver(provider).run("run pwd")
    assert runs.commands(spec=False) == [(HOOK_EVENT_PRE_TOOL_USE, "audit")]


@pytest.mark.asyncio
async def test_on_kiro_cli_hooks_page_tool_hooks_meet_the_harness_tool_name(
    tmp_path, runs, store, wire
):
    """kiro-cli names the call ``shell`` and titles it with the command. A Hooks-page
    hook scoped to ``shell`` meets both of its fires, though Crew learns no spec
    identity on that backend."""
    _write_spec({})
    for event, command in ((HOOK_EVENT_PRE_TOOL_USE, "audit"), (HOOK_EVENT_POST_TOOL_USE, "watch")):
        store.create({"name": command, "event": event, "matcher": "shell", "command": command})
    events = [
        LLMEvent(kind=EVENT_TOOL_CALL, title="echo hi", tool_call_id="t1", tool_name="shell"),
        LLMEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="t1",
            tool_output="hi",
            tool_final=True,
            tool_status="completed",
        ),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    provider = _Provider(events, tmp_path, backend=ACP_BACKEND_KIRO)
    await _driver(provider).run("say hi")
    assert runs.commands(spec=False) == [
        (HOOK_EVENT_PRE_TOOL_USE, "audit"),
        (HOOK_EVENT_POST_TOOL_USE, "watch"),
    ]


@pytest.mark.asyncio
async def test_a_reused_session_gates_on_the_agent_it_runs_not_the_resolved_one(
    tmp_path, runs, store, wire
):
    """After a default-agent switch elsewhere the channel resolves another agent for
    this thread, but the reused session still runs the guarded one: its guard gates."""
    _write_spec(_SPEC_HOOKS)
    agents = paths_mod.kiro_agents_dir()
    (agents / "unguarded.json").write_text(
        json.dumps({"name": "unguarded", "prompt": "p", "tools": ["*"]}), encoding="utf-8"
    )
    runs.exits["guard"] = 2
    provider = _Provider(_shell_call(finished=False), tmp_path)
    await _driver(provider, hook_agent="unguarded").run("run touch")
    assert runs.commands(spec=True) == [(HOOK_EVENT_PRE_TOOL_USE, "guard")]
    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_a_new_session_runs_agent_spawn_once_into_the_hook_context(
    tmp_path, runs, store, wire
):
    _write_spec(_SPEC_HOOKS)
    runs.stdout["spawn"] = "SPAWN-MARKER-42"
    provider = _Provider([LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")], tmp_path)
    await _driver(provider, is_new_session=True).run("hello")
    assert runs.commands(spec=True) == [(HOOK_EVENT_AGENT_SPAWN, "spawn")]
    assert provider.messages[0].startswith("[Hook context]\nSPAWN-MARKER-42\n")


@pytest.mark.asyncio
async def test_a_resumed_session_runs_no_agent_spawn(tmp_path, runs, store, wire):
    _write_spec(_SPEC_HOOKS)
    provider = _Provider([LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")], tmp_path)
    await _driver(provider).run("hello")
    assert runs.seen == []
    assert provider.messages == ["hello"]


@pytest.mark.asyncio
async def test_on_kiro_cli_no_spec_hook_fires_but_the_hooks_page_guard_still_blocks(
    tmp_path, runs, store, wire
):
    """kiro-cli runs the spec's hooks itself, so Crew fires none of them there. The
    Hooks page's PreToolUse hook is Crew's own and gates the call on every backend."""
    _write_spec(_SPEC_HOOKS)
    store.create({"name": "store-guard", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "deny"})
    runs.exits["deny"] = 2
    provider = _Provider(_shell_call(finished=True), tmp_path, backend=ACP_BACKEND_KIRO)
    await _driver(provider, is_new_session=True).run("run touch")
    assert runs.commands(spec=True) == []
    # Once as the call's informational frame, once as its gate.
    assert runs.commands(spec=False) == [
        (HOOK_EVENT_PRE_TOOL_USE, "deny"),
        (HOOK_EVENT_PRE_TOOL_USE, "deny"),
    ]
    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_an_unreadable_spec_refuses_every_request(tmp_path, runs, store, wire):
    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{_AGENT}.json").write_text("{not json", encoding="utf-8")
    provider = _Provider(_shell_call(finished=False), tmp_path)
    await _driver(provider).run("run touch")
    assert runs.seen == []
    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
async def test_a_turn_the_operators_hooks_skip_still_gates_and_runs_the_spec(
    tmp_path, runs, store, wire
):
    """A room turn runs none of the Hooks page's observer hooks, but its PreToolUse
    guard gates every call, and the spec's own hooks run as the harness would."""
    _write_spec(_SPEC_HOOKS)
    store.create({"name": "store-pre", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "audit"})
    store.create({"name": "store-post", "event": HOOK_EVENT_POST_TOOL_USE, "command": "watch"})
    provider = _Provider(_shell_call(finished=True), tmp_path)
    await _driver(provider, operator=False).run("run touch")
    assert provider.approved == ["r1"]
    assert runs.commands(spec=False) == [(HOOK_EVENT_PRE_TOOL_USE, "audit")]
    assert runs.commands(spec=True) == [
        (HOOK_EVENT_PRE_TOOL_USE, "guard"),
        (HOOK_EVENT_POST_TOOL_USE, "post"),
    ]


@pytest.mark.asyncio
async def test_on_kiro_cli_a_room_turn_runs_the_hooks_page_pre_tool_use_only_as_its_gate(
    tmp_path, runs, store, wire
):
    """The tool-call frame's PreToolUse cannot refuse the call, so a turn the
    operator's hooks skip runs none; the permission request's gate still runs."""
    _write_spec(_SPEC_HOOKS)
    store.create({"name": "store-guard", "event": HOOK_EVENT_PRE_TOOL_USE, "command": "deny"})
    runs.exits["deny"] = 2
    provider = _Provider(_shell_call(finished=True), tmp_path, backend=ACP_BACKEND_KIRO)
    await _driver(provider, operator=False).run("run touch")
    assert runs.commands(spec=False) == [(HOOK_EVENT_PRE_TOOL_USE, "deny")]
    assert provider.approved == []
    assert provider.rejected == ["r1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [True, False])
async def test_the_users_hook_files_observe_only_the_operators_own_turn(
    tmp_path, monkeypatch, runs, store, wire, operator
):
    """KAS folds the user's ``~/.kiro/hooks`` files into the spec's hooks. They are
    the operator's, as the Hooks page's are: on a room turn only their PreToolUse
    guard runs, and none of their output reaches the prompt the room's sender gets."""
    _write_spec(_SPEC_HOOKS)
    _write_user_hook_file(tmp_path, monkeypatch)
    runs.stdout["file-spawn"] = "OPERATOR-PRIVATE-SPAWN"
    runs.stdout["file-prompt"] = "OPERATOR-PRIVATE-PROMPT"
    provider = _Provider(_shell_call(finished=True), tmp_path)
    await _driver(provider, operator=operator, is_new_session=True).run("run touch")
    assert provider.approved == ["r1"]
    # The agent's own hooks run either way.
    assert [(e, c) for e, c in runs.commands(spec=True) if not c.startswith("file-")] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn"),
        (HOOK_EVENT_PRE_TOOL_USE, "guard"),
        (HOOK_EVENT_POST_TOOL_USE, "post"),
    ]
    if operator:
        assert _file_runs(runs) == [
            (HOOK_EVENT_AGENT_SPAWN, "file-spawn"),
            (HOOK_EVENT_USER_PROMPT_SUBMIT, "file-prompt"),
            (HOOK_EVENT_PRE_TOOL_USE, "file-guard"),
            (HOOK_EVENT_POST_TOOL_USE, "file-post"),
            (HOOK_EVENT_STOP, "file-stop"),
        ]
        assert "OPERATOR-PRIVATE-SPAWN" in provider.messages[0]
        assert "OPERATOR-PRIVATE-PROMPT" in provider.messages[0]
    else:
        assert _file_runs(runs) == [(HOOK_EVENT_PRE_TOOL_USE, "file-guard")]
        assert "OPERATOR-PRIVATE" not in provider.messages[0]


@pytest.mark.asyncio
async def test_a_guest_turn_reads_no_spec_and_runs_no_hook(
    tmp_path, monkeypatch, runs, store, wire
):
    """A ``deny_all_tools`` turn refuses every call before any hook could be asked,
    and its tool-less agent mounts no hook: nothing the operator or a spec saved
    runs, and nothing reaches the guest's prompt."""
    _write_spec(_SPEC_HOOKS)
    _write_user_hook_file(tmp_path, monkeypatch)
    runs.stdout["file-prompt"] = "OPERATOR-PRIVATE-PROMPT"
    store.create({"name": "store-prompt", "event": HOOK_EVENT_USER_PROMPT_SUBMIT, "command": "sp"})
    provider = _Provider(_shell_call(finished=True), tmp_path)
    await _driver(provider, operator=False, deny_all_tools=True, is_new_session=True).run(
        "run touch"
    )
    assert runs.seen == []
    assert provider.messages == ["run touch"]
    assert provider.approved == []
    assert provider.rejected == ["r1"]
