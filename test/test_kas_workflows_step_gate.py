"""A workflow step meets the chat turn's tool gate before any allow path.

``policy.step_gate`` runs ``HookManager.on_tool_call`` (governance ceiling,
``auto_deny_tools``, every deny tier), the Hooks page's PreToolUse hooks and a
moved Bot's held rules over the step's request, under the owning chat's key. A
deny or a hook block rejects the step; a held rule keeps it for a person. No
auto-approve path answers past it: Trust, Trust reads, a saved "Always" grant
and the mode-change sweep, which also runs every admission screen again.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from kas_workflow_helpers import FakeRuntime, make_router, stop_step

from kiro_crew.kas_workflows import wire
from kiro_crew.testing.wait import async_wait_until

PERM = "session/request_permission"
_OPTIONS = [
    {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
    {"optionId": "always", "kind": "allow_always", "name": "Always"},
    {"optionId": "no", "kind": "reject_once", "name": "Reject"},
]


def _shell(command: str = "ls", **kiro: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sessionId": "s0",
        "toolCall": {
            "toolCallId": "tc-1",
            "title": f"Run {command}",
            "rawInput": {"command": command},
        },
        "options": list(_OPTIONS),
    }
    if kiro:
        params["_meta"] = {"kiro": kiro}
    return params


class _Rules:
    """Which of the gate's three checks refuses right now (all off to start)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
        self.kind = kind
        self.on = False
        self.seen_keys: list[str] = []
        from kiro_crew import hooks as hooks_mod
        from kiro_crew import mate_held_rules

        real_gate = hooks_mod.HookManager.on_tool_call
        real_pre = hooks_mod.permission_pre_tool_block

        def _gate(manager, title, **kwargs):
            self.seen_keys.append(kwargs.get("session_key", ""))
            if self.on and kind == "gate-deny":
                return hooks_mod.ToolHookResult.deny_policy("Blocked by governance policy")
            return real_gate(manager, title, **kwargs)

        async def _pre(*args, **kwargs):
            if self.on and kind == "pretool-block":
                return "a PreToolUse hook said no"
            return await real_pre(*args, **kwargs)

        def _held(event, *, session_key="", agent=""):
            return ["ask before any shell"] if self.on and kind == "held-rule" else []

        monkeypatch.setattr(hooks_mod.HookManager, "on_tool_call", _gate)
        monkeypatch.setattr(hooks_mod, "permission_pre_tool_block", _pre)
        monkeypatch.setattr(mate_held_rules, "held_for", _held)


async def _router(mode: str, **kwargs: Any):
    audits: list[dict[str, Any]] = []
    router = make_router(audit=kwargs.pop("audit", lambda **kw: audits.append(kw)), **kwargs)
    await router.ensure_ledger()
    router._ledger["w0"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0}
    current = [mode]
    router.note_chat_session("chat-1", None, mode=lambda: (current[0], current[0]))
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w0", "nodeTree": [{"type": "step", "id": "a", "label": "Do"}]},
    )
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    return router, rt, sink, current, audits


def _sent(rt: FakeRuntime) -> list[str | None]:
    return [result.get("outcome", {}).get("optionId") for _rid, result in rt.responses]


_KINDS = ("gate-deny", "pretool-block", "held-rule")
_SITES = ("trust", "trust_reads", "grant-use", "mode-sweep", "person-answer")
#: A rule that lands while a step is held is judged again by the sweep and a
#: person's allow, except the PreToolUse hooks: they fire once per request, as on
#: the chat path, which re-judges a held call on the gate alone.
_LATE = ("mode-sweep", "person-answer")


def _expected(kind: str, site: str) -> str:
    if kind == "held-rule":
        return "yes" if site == "person-answer" else "held"
    if kind == "pretool-block" and site in _LATE:
        return "yes"
    return "no"


@pytest.mark.parametrize("site", _SITES)
@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.asyncio
async def test_no_allow_path_answers_a_step_the_tool_gate_stops(monkeypatch, kind, site):
    from kiro_crew.kas_workflows.router import KasWorkflowError

    rules = _Rules(monkeypatch, kind)
    start = "normal" if site in ("grant-use",) + _LATE else site
    router, rt, sink, mode, _ = await _router(start)
    if site == "grant-use":
        # The person picked "Always" for this command while nothing refused it.
        sink.on_step_request(9, "s0", PERM, _shell())
        await async_wait_until(lambda: router.approvals())
        await router.answer(router.approvals()[0]["key"], "always")
        rt.responses.clear()
    if site not in _LATE:
        rules.on = True
    sink.on_step_request(1, "s0", PERM, _shell())
    if site in _LATE:
        # Held under Normal while nothing refused it; then the rule lands.
        await async_wait_until(lambda: router.approvals())
        rules.on = True
        if site == "mode-sweep":
            mode[0] = "trust"
            await router.recheck_pending()
        else:
            try:
                await router.answer(router.approvals()[0]["key"], "yes")
            except KasWorkflowError:
                pass
    expected = _expected(kind, site)
    if expected == "held":
        await async_wait_until(lambda: router.approvals())
        assert _sent(rt) == [], "a held rule waits for a person"
        assert len(router.approvals()) == 1
    else:
        await async_wait_until(lambda: rt.responses)
        assert _sent(rt) == [expected]
        assert router.approvals() == []
    assert "chat-1" in rules.seen_keys, "judged under the owning chat's key"


@pytest.mark.parametrize("site", ("trust", "trust_reads"))
@pytest.mark.asyncio
async def test_a_step_the_gate_passes_still_auto_approves(monkeypatch, site):
    _Rules(monkeypatch, "gate-deny")
    router, rt, sink, _, audits = await _router(site)
    sink.on_step_request(2, "s0", PERM, _shell())
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["yes"]
    assert [a["outcome"] for a in audits] == ["auto_approved"]


@pytest.mark.asyncio
async def test_a_gate_that_raises_refuses_the_step():
    async def _broken(_event, _session_key, **_kw):
        raise RuntimeError("gate down")

    router, rt, sink, _, audits = await _router("trust", gate=_broken)
    sink.on_step_request(3, "s0", PERM, _shell())
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"]
    assert audits[-1]["metadata"]["reason"] == "tool gate: the tool gate could not run"


@pytest.mark.parametrize("screen", ("deny-rule", "input"))
@pytest.mark.asyncio
async def test_the_sweep_runs_every_admission_screen_again(screen):
    refuse = [False]
    router, rt, sink, mode, _ = await _router(
        "normal",
        screen=lambda _c: "denied" if refuse[0] and screen == "deny-rule" else None,
        input_screen=lambda *_a: "sensitive path" if refuse[0] and screen == "input" else None,
    )
    sink.on_step_request(4, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    refuse[0] = True  # the operator added a rule while it waited
    mode[0] = "trust"
    assert await router.recheck_pending() == 0
    assert _sent(rt) == ["no"] and router.approvals() == []


@pytest.mark.asyncio
async def test_an_always_on_a_call_with_no_command_or_tool_id_grants_nothing():
    # The title is the step model's display text, not an identity: a grant keyed
    # on it would let a different tool with the same title through.
    router, rt, sink, _, _ = await _router("normal")
    bare = {
        "sessionId": "s0",
        "toolCall": {"toolCallId": "tc-1", "title": "Do the thing", "rawInput": {}},
        "options": list(_OPTIONS),
    }
    sink.on_step_request(5, "s0", PERM, bare)
    await async_wait_until(lambda: router.approvals())
    [card] = router.approvals()
    [always] = [o for o in card["options"] if o["kind"] == "allow_always"]
    assert always["grant"] == {"scope": "once", "match": None}, "the card says it is once"
    await router.answer(card["key"], "always")
    assert _sent(rt) == ["yes"], "allowed this once"
    sink.on_step_request(
        6, "s0", PERM, dict(bare, toolCall=dict(bare["toolCall"], toolCallId="tc-2"))
    )
    await async_wait_until(lambda: router.approvals())
    assert _sent(rt) == ["yes"], "the next call with that title asks again"


# ── One trusted identity, derived as on a chat turn ──


def _frame(update: dict[str, Any]) -> dict[str, Any]:
    return {"sessionId": "s0", "update": update}


_EDIT_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "tc-edit",
    "title": "Write File",
    "kind": "edit",
    "status": "in_progress",
    "rawInput": {"path": "notes/out.txt", "text": "x"},
    "locations": [{"path": "notes/out.txt"}],
    "_meta": {"kiro": {"toolOrigin": "default"}},
}
_EDIT_REQ = {
    "sessionId": "s0",
    "toolCall": {"toolCallId": "tc-edit", "status": "pending", "title": "Write File"},
    "options": list(_OPTIONS),
    "_meta": {
        "kiro": {
            "toolId": "write_file",
            "consent": {"capability": "fs_write", "resource": "notes/out.txt"},
        }
    },
}
_SHELL_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "tc-sh",
    "title": "Run Command",
    "kind": "execute",
    "status": "pending",
    "rawInput": {"command": "make build"},
    "_meta": {"kiro": {"toolOrigin": "default"}},
}
_SHELL_REQ = {
    "sessionId": "s0",
    "toolCall": {"toolCallId": "tc-sh", "status": "pending", "title": "make build"},
    "options": list(_OPTIONS),
    "_meta": {
        "kiro": {
            "toolId": "run_command",
            "command": "make build",
            "consent": {"capability": "shell", "resource": "make build"},
        }
    },
}
_MCP_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "tc-mcp",
    "title": "probe_echo",
    "kind": "other",
    "status": "in_progress",
    "rawInput": {"message": "hi"},
    "_meta": {"kiro": {"mcpServerName": "probe", "toolName": "probe_echo"}},
}
_MCP_REQ = {
    "sessionId": "s0",
    "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "@probe/probe_echo"},
    "options": list(_OPTIONS),
    "_meta": {
        "kiro": {
            "toolId": "mcp_probe_probe_echo",
            "consent": {"capability": "mcp", "resource": "probe/probe_echo"},
        }
    },
}


def _deny_tools(monkeypatch: pytest.MonkeyPatch, *globs: str) -> None:
    from kiro_crew import hooks as hooks_mod

    monkeypatch.setattr(
        hooks_mod,
        "hooks_config_from_config_dict",
        lambda _d: hooks_mod.HooksConfig(auto_deny_tools=list(globs)),
    )


async def _no_name_refusal(_command: str):
    return None


async def _real_identity_router(mode: str = "trust"):
    # identity=None: the router's own caches, fed by the step's frames.
    # The name-grant check resolves program names on this host's PATH; it is not
    # what these tests judge, so it passes (its own tests cover it).
    return await _router(mode, identity=None, name_grant=_no_name_refusal)


async def _ask_framed(sink, rid: int, frame: dict[str, Any], req: dict[str, Any]) -> None:
    sink.on_watched_notification("session/update", _frame(frame))
    sink.on_step_request(rid, "s0", PERM, req)


@pytest.mark.parametrize(
    "case",
    ("write-tool-deny", "governance-execute_bash-deny", "mcp-identity-deny"),
)
@pytest.mark.asyncio
async def test_a_rule_on_the_tools_identity_stops_a_trusted_step(monkeypatch, case):
    if case == "write-tool-deny":
        # KAS's own id for its file write, the spelling the chat turn's gate
        # refuses (test_the_step_gate_reads_what_a_chat_turn_reads).
        _deny_tools(monkeypatch, "write_file")
        frame, req = _EDIT_FRAME, _EDIT_REQ
    elif case == "mcp-identity-deny":
        _deny_tools(monkeypatch, "@probe/probe_echo")
        frame, req = _MCP_FRAME, _MCP_REQ
    else:
        import dataclasses

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.platform import context as ctx_mod
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.governance import parse_policy

        base = build_default_context(KiroCrewConfig.load())
        body = {
            "version": 1,
            "boot": {"fail_closed": True},
            "tools": {"mode": "deny", "deny": ["execute_bash"]},
        }
        ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(body)))
        monkeypatch.setattr(ctx_mod, "_restore_marker", None, raising=False)
        frame, req = _SHELL_FRAME, _SHELL_REQ
    try:
        router, rt, sink, _, audits = await _real_identity_router()
        await _ask_framed(sink, 1, frame, req)
        await async_wait_until(lambda: rt.responses)
        assert _sent(rt) == ["no"], "the rule on the tool's identity refuses it under Trust"
        assert audits[-1]["metadata"]["reason"].startswith("tool gate: ")
    finally:
        if case == "governance-execute_bash-deny":
            from kiro_crew.platform import context as ctx_mod

            ctx_mod.reset_context()


@pytest.mark.parametrize(
    "frame,req", ((_EDIT_FRAME, _EDIT_REQ), (_SHELL_FRAME, _SHELL_REQ), (_MCP_FRAME, _MCP_REQ))
)
@pytest.mark.asyncio
async def test_a_trusted_step_with_a_verified_identity_still_auto_approves(frame, req):
    router, rt, sink, _, _ = await _real_identity_router()
    await _ask_framed(sink, 1, frame, req)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["yes"]


@pytest.mark.parametrize("mode", ("trust", "normal"))
@pytest.mark.asyncio
async def test_a_step_whose_tool_call_frame_never_arrived_is_refused(mode):
    router, rt, sink, _, audits = await _real_identity_router(mode)
    sink.on_step_request(1, "s0", PERM, _SHELL_REQ)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"] and router.approvals() == []
    assert audits[-1]["metadata"]["reason"].startswith("identity: ")


@pytest.mark.asyncio
async def test_a_frame_from_another_session_names_nothing():
    router, rt, sink, _, _ = await _real_identity_router()
    sink.on_watched_notification(
        "session/update", {"sessionId": "chat-acp", "update": _SHELL_FRAME}
    )
    sink.on_step_request(1, "s0", PERM, _SHELL_REQ)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"]


def _plan_request(title: str = "Approve the plan") -> dict[str, Any]:
    return {
        "sessionId": "s0",
        "toolCall": {"toolCallId": "tc-plan", "title": title, "rawInput": {}},
        "options": list(_OPTIONS),
        "_meta": {"kiro": {"toolId": "update_workflow"}},
    }


@pytest.mark.asyncio
async def test_a_plan_handoff_meets_the_gate_and_is_never_auto_approved(monkeypatch):
    rules = _Rules(monkeypatch, "gate-deny")
    router, rt, sink, _, _ = await _router("trust")
    sink.on_step_request(1, "s0", PERM, _plan_request())
    await async_wait_until(lambda: router.approvals())
    assert _sent(rt) == [], "a plan waits for the person under Trust"
    [card] = router.approvals()
    pending = router._pending[card["key"]]
    assert pending.raw_params is None, "nothing of a plan request is kept"
    rules.on = True
    sink.on_step_request(2, "s0", PERM, _plan_request("Approve another plan"))
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"], "a plan the gate refuses is never shown"


@pytest.mark.asyncio
async def test_a_persons_answer_is_rechecked_and_logged(monkeypatch):
    from kiro_crew.kas_workflows.router import KasWorkflowError

    rules = _Rules(monkeypatch, "gate-deny")
    router, rt, sink, _, audits = await _router("normal")
    sink.on_step_request(1, "s0", PERM, _shell("ls"))
    sink.on_step_request(
        2,
        "s0",
        PERM,
        dict(_shell("pwd"), toolCall=dict(_shell("pwd")["toolCall"], toolCallId="tc-2")),
    )
    await async_wait_until(lambda: len(router.approvals()) == 2)
    first, second = router.approvals()
    await router.answer(first["key"], "no")
    assert audits[-1]["outcome"] == "rejected" and audits[-1]["metadata"]["reason"] == "person"
    rules.on = True  # a rule lands while the second waits
    with pytest.raises(KasWorkflowError):
        await router.answer(second["key"], "yes")
    assert _sent(rt) == ["no", "no"], "the allow was refused, never sent"
    rules.on = False
    sink.on_step_request(
        3,
        "s0",
        PERM,
        dict(_shell("id"), toolCall=dict(_shell("id")["toolCall"], toolCallId="tc-3")),
    )
    await async_wait_until(lambda: router.approvals())
    await router.answer(router.approvals()[0]["key"], "yes")
    assert _sent(rt)[-1] == "yes"
    assert audits[-1]["outcome"] == "approved" and audits[-1]["metadata"]["reason"] == "person"


@pytest.mark.asyncio
async def test_an_allow_that_cannot_be_logged_is_not_sent():
    from kiro_crew.kas_workflows.router import KasWorkflowError

    def _audit(**kw):
        if kw.get("outcome") == "approved":
            raise OSError("log down")

    router, rt, sink, _, _ = await _router("normal", audit=_audit)
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    with pytest.raises(KasWorkflowError):
        await router.answer(router.approvals()[0]["key"], "yes")
    assert _sent(rt) == [] and len(router.approvals()) == 1, "left held for the person"


@pytest.mark.asyncio
async def test_a_held_step_keeps_only_a_bounded_snapshot():
    from kiro_crew.kas_workflows import policy

    router, rt, sink, _, _ = await _router("normal")
    big = dict(_shell(), content="x" * (policy.MAX_SNAPSHOT_CHARS * 4))
    sink.on_step_request(1, "s0", PERM, big)
    await async_wait_until(lambda: router.approvals())
    [held] = router._pending.values()
    assert "content" not in (held.raw_params or {}), "fields no screen reads are not kept"
    over = dict(
        _shell(), toolCall=dict(_shell()["toolCall"], title="x" * policy.MAX_SNAPSHOT_CHARS)
    )
    assert policy.screen_snapshot(over) is None, "a request too large to snapshot keeps none"


@pytest.mark.asyncio
async def test_a_request_that_names_a_command_is_never_a_question(monkeypatch):
    rules = _Rules(monkeypatch, "gate-deny")
    rules.on = True
    router, rt, sink, _, _ = await _router("normal")
    only_once = dict(
        _shell("rm notes.txt"), options=[{"optionId": "a", "kind": "allow_once", "name": "Yes"}]
    )
    sink.on_step_request(1, "s0", PERM, only_once)
    await async_wait_until(lambda: rt.responses or router.questions() or router.approvals())
    assert router.questions() == [], "not held as a question"
    assert rt.responses and router.approvals() == [], "the gate refused it"


@pytest.mark.asyncio
async def test_the_sweep_does_not_fire_the_pretooluse_hooks_again(monkeypatch):
    from kiro_crew import hooks as hooks_mod

    fired: list[str] = []

    async def _pre(*args, **kwargs):
        fired.append(args[3])
        return None

    monkeypatch.setattr(hooks_mod, "permission_pre_tool_block", _pre)
    router, rt, sink, mode, _ = await _router("normal")
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    mode[0] = "trust"
    assert await router.recheck_pending() == 1
    assert len(fired) == 1, "fired once, at admission"


def test_a_steps_note_names_its_agent_redacted():
    from kiro_crew.kas_workflows import router as router_mod

    written: list[dict[str, Any]] = []
    router = make_router()
    router._write_marker = lambda _slot, event: written.append(dict(event)) or True
    secret = "sk-" + "proj-" + "a1B2c3D4e5" * 4
    router._mark_note("w0", {"nodeId": "a", "message": "hi", "agentName": secret}, "chat-1")
    [event] = written
    assert event["agent"] == router_mod._redact(secret) and secret not in event["agent"]


@pytest.mark.parametrize("globs", (["fs_write"], ["write_file"], ["Write File"], ["execute_bash"]))
def test_the_step_gate_reads_what_a_chat_turn_reads(globs):
    # The step's event and a chat turn's event for the same KAS call get the same
    # gate inputs and the same verdict, whatever the rule.
    from test_tool_call_classification_kas import _kas_builtin_flow

    from kiro_crew.hooks import HookManager, HooksConfig, hook_gate_kwargs
    from kiro_crew.kas_workflows.step_identity import StepToolCalls

    chat = _kas_builtin_flow(
        "write_file",
        title="Write File",
        kind="edit",
        raw_input={"path": "notes/out.txt", "text": "x"},
    )
    calls = StepToolCalls()
    calls.observe("s0", _EDIT_FRAME)
    step, why = calls.event_for(_EDIT_REQ)
    assert why is None
    assert hook_gate_kwargs(step) == hook_gate_kwargs(chat)
    gate = HookManager(HooksConfig(auto_deny_tools=list(globs)))
    assert (
        gate.on_tool_call(step.title, **hook_gate_kwargs(step)).action
        == gate.on_tool_call(chat.title, **hook_gate_kwargs(chat)).action
    )


@pytest.mark.asyncio
async def test_trust_reads_reads_the_command_off_the_identity_not_the_request():
    # A non-shell (MCP) call whose input merely has a ``cmd`` key is no read: the
    # chat turn reads a read from the shell call's own input.
    router, rt, sink, _, audits = await _real_identity_router("trust_reads")
    frame = dict(_MCP_FRAME, rawInput={"cmd": "ls", "message": "hi"})
    req = dict(_MCP_REQ, toolCall=dict(_MCP_REQ["toolCall"], rawInput={"cmd": "ls"}))
    await _ask_framed(sink, 1, frame, req)
    await async_wait_until(lambda: router.approvals() or rt.responses)
    assert _sent(rt) == [] and audits == [], "held for a person, never auto-approved"
    # A real shell read on the same chat still is.
    read_frame = dict(_SHELL_FRAME, toolCallId="tc-ls", rawInput={"command": "ls"})
    read_req = dict(
        _SHELL_REQ,
        toolCall=dict(_SHELL_REQ["toolCall"], toolCallId="tc-ls", title="ls"),
        _meta={"kiro": {"toolId": "run_command", "command": "ls"}},
    )
    await _ask_framed(sink, 2, read_frame, read_req)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["yes"]


_PLAN_FRAME = {
    "sessionUpdate": "tool_call",
    "toolCallId": "tc-plan",
    "title": "Update Workflow",
    "kind": "other",
    "status": "pending",
    "rawInput": {"workflowRef": "wf.yaml"},
    "_meta": {"kiro": {"toolOrigin": "default"}},
}


@pytest.mark.asyncio
async def test_a_plan_handoff_is_judged_on_its_frames_identity(monkeypatch):
    plan = dict(_plan_request(), toolCall={"toolCallId": "tc-plan", "title": "Update Workflow"})
    # No frame: no identity, refused (never shown as a card).
    router, rt, sink, _, audits = await _real_identity_router("normal")
    sink.on_step_request(1, "s0", PERM, plan)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"] and audits[-1]["metadata"]["reason"].startswith("identity: ")
    # With its frame it is held for the person ...
    await _ask_framed(sink, 2, _PLAN_FRAME, plan)
    await async_wait_until(lambda: router.approvals())
    # ... and a rule on its tool's own id refuses it.
    _deny_tools(monkeypatch, "update_workflow")
    await _ask_framed(sink, 3, _PLAN_FRAME, plan)
    await async_wait_until(lambda: len(rt.responses) == 2)
    assert _sent(rt) == ["no", "no"]


@pytest.mark.asyncio
async def test_a_frame_from_another_runtime_names_nothing():
    router, rt, sink, _, _ = await _real_identity_router()
    other = router.sink_for(FakeRuntime())
    other.on_watched_notification("session/update", _frame(_SHELL_FRAME))
    sink.on_step_request(1, "s0", PERM, _SHELL_REQ)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"], "only the step's own runtime names its calls"


def test_a_steps_note_keeps_its_node_and_severity_bounded():
    from kiro_crew.kas_workflows import router as router_mod

    written: list[dict[str, Any]] = []
    router = make_router()
    router._write_marker = lambda _slot, event: written.append(dict(event)) or True
    secret = "sk-" + "proj-" + "a1B2c3D4e5" * 4
    router._mark_note("w0", {"nodeId": secret, "message": "hi", "severity": "x" * 500}, "chat-1")
    [event] = written
    assert event["nodeId"] == router_mod._redact(secret)[:200]
    assert event["severity"] == "info"


# ── An "Always" grant is bound to the verified identity ──


async def _always(router, rt, sink, rid: int, frame, req) -> None:
    await _ask_framed(sink, rid, frame, req)
    await async_wait_until(lambda: router.approvals())
    await router.answer(router.approvals()[0]["key"], "always")
    assert _sent(rt)[-1] == "yes"


def _mcp(server: str, tool: str, call_id: str, raw: dict[str, Any] | None = None):
    fused = f"mcp_{server}_{tool}"
    raw = {"message": "hi"} if raw is None else raw
    frame = dict(
        _MCP_FRAME,
        toolCallId=call_id,
        title=tool,
        rawInput=raw,
        _meta={"kiro": {"mcpServerName": server, "toolName": tool}},
    )
    req = dict(
        _MCP_REQ,
        toolCall={
            "toolCallId": call_id,
            "status": "pending",
            "title": f"@{server}/{tool}",
            "rawInput": raw,
        },
        _meta={
            "kiro": {
                "toolId": fused,
                "consent": {"capability": "mcp", "resource": f"{server}/{tool}"},
            }
        },
    )
    return frame, req


@pytest.mark.asyncio
async def test_a_shell_always_does_not_cover_an_mcp_call_naming_the_same_command():
    router, rt, sink, _, _ = await _real_identity_router("normal")
    await _always(router, rt, sink, 1, _SHELL_FRAME, _SHELL_REQ)
    frame, req = _mcp("probe", "probe_echo", "tc-m", {"command": "make build"})
    await _ask_framed(sink, 2, frame, req)
    await async_wait_until(lambda: router.approvals())
    assert _sent(rt) == ["yes"], "the MCP call asks: the grant was for the shell command"


@pytest.mark.asyncio
async def test_an_mcp_always_does_not_cover_a_tool_with_the_same_fused_id():
    # Calls with no arguments: the only MCP calls a chat turn's "Always" binds.
    router, rt, sink, _, _ = await _real_identity_router("normal")
    first, first_req = _mcp("a_b", "c", "tc-1", {})
    await _always(router, rt, sink, 1, first, first_req)
    other, other_req = _mcp("a", "b_c", "tc-2", {})  # KAS fuses both to mcp_a_b_c
    await _ask_framed(sink, 2, other, other_req)
    await async_wait_until(lambda: router.approvals())
    assert _sent(rt) == ["yes"], "a different tool asks"
    again, again_req = _mcp("a_b", "c", "tc-3", {})
    await _ask_framed(sink, 3, again, again_req)
    await async_wait_until(lambda: len(rt.responses) == 2)
    assert _sent(rt) == ["yes", "yes"], "the granted tool itself is covered"


@pytest.mark.asyncio
async def test_a_step_moved_to_another_runtime_forgets_its_old_frames():
    router, rt, sink, _, _ = await _real_identity_router()
    sink.on_watched_notification("session/update", _frame(_SHELL_FRAME))
    moved = FakeRuntime()
    moved_sink = router.sink_for(moved)
    moved_sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    moved_sink.on_step_request(1, "s0", PERM, _SHELL_REQ)
    await async_wait_until(lambda: moved.responses)
    assert [r.get("outcome", {}).get("optionId") for _i, r in moved.responses] == ["no"]


@pytest.mark.parametrize("raw", ({"command": "make build"}, {"message": "hi"}))
@pytest.mark.asyncio
async def test_an_always_on_an_mcp_call_with_arguments_is_one_call(raw):
    # A chat turn's "Always" binds no non-shell call with arguments
    # (trust_patterns.approval_command), and the card says so.
    router, rt, sink, _, _ = await _real_identity_router("normal")
    frame, req = _mcp("probe", "probe_echo", "tc-1", raw)
    await _ask_framed(sink, 1, frame, req)
    await async_wait_until(lambda: router.approvals())
    [card] = router.approvals()
    [always] = [o for o in card["options"] if o["kind"] == "allow_always"]
    assert always["grant"] == {"scope": "once", "match": None}
    await router.answer(card["key"], "always")
    frame2, req2 = _mcp("probe", "probe_echo", "tc-2", {"message": "something else"})
    await _ask_framed(sink, 2, frame2, req2)
    await async_wait_until(lambda: router.approvals())
    assert _sent(rt) == ["yes"], "the next call asks again"


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_step_a_person_is_answering():
    import asyncio
    import threading

    gate = threading.Event()
    audits: list[str] = []

    def _audit(**kw):
        audits.append(kw.get("outcome"))
        if kw.get("outcome") == "rejected":
            gate.wait(30)  # the reject's log write waits on the test's signal

    router, rt, sink, mode, _ = await _router("normal", audit=_audit)
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    answering = asyncio.ensure_future(router.answer(router.approvals()[0]["key"], "no"))
    await async_wait_until(lambda: "rejected" in audits)
    mode[0] = "trust"
    assert await router.recheck_pending() == 0, "the sweep leaves it to the person"
    gate.set()
    await answering
    assert _sent(rt) == ["no"], "the person's reject stands"


@pytest.mark.asyncio
async def test_an_always_on_a_file_write_is_one_call():
    # A chat turn binds a one-file grant only after its link check on the path;
    # a step's grant runs no such check, so it grants nothing.
    # KAS's delete_file on an absolute, normal path: exactly what a chat turn's
    # one-file grant (trust_patterns.PATH_TRUST_TOOLS) could name.
    import tempfile
    from pathlib import Path

    target = str(Path(tempfile.gettempdir()).resolve() / "out.txt")
    frame = dict(
        _EDIT_FRAME,
        title="Delete File",
        kind="delete",
        rawInput={"path": target},
        locations=[{"path": target}],
    )
    kiro = {"toolId": "delete_file", "consent": {"capability": "fs_write", "resource": target}}
    req = dict(_EDIT_REQ, _meta={"kiro": kiro})
    router, rt, sink, _, _ = await _real_identity_router("normal")
    await _ask_framed(sink, 1, frame, req)
    await async_wait_until(lambda: router.approvals())
    [card] = router.approvals()
    [always] = [o for o in card["options"] if o["kind"] == "allow_always"]
    assert always["grant"] == {"scope": "once", "match": None}
    await router.answer(card["key"], "always")
    await _ask_framed(sink, 2, frame, req)
    await async_wait_until(lambda: router.approvals())
    assert _sent(rt) == ["yes"], "the same write asks again"


@pytest.mark.asyncio
async def test_a_sweep_already_judging_a_step_leaves_it_to_a_persons_reject():
    import asyncio
    import threading

    hold = threading.Event()
    in_sweep = threading.Event()
    blocking = [False]

    def _input_screen(_event, _params):
        if blocking[0]:
            in_sweep.set()
            hold.wait(30)
        return None

    logging_reject = threading.Event()
    release_reject = threading.Event()

    def _audit(**kw):
        if kw.get("outcome") == "rejected":
            logging_reject.set()
            release_reject.wait(30)

    router, rt, sink, mode, _ = await _router("normal", input_screen=_input_screen, audit=_audit)
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    key = router.approvals()[0]["key"]
    blocking[0] = True
    mode[0] = "trust"
    sweep = asyncio.ensure_future(router.recheck_pending())
    await async_wait_until(in_sweep.is_set)
    # The person rejects while the sweep is mid-judgement; the reject is still
    # being logged when the sweep resumes.
    answering = asyncio.ensure_future(router.answer(key, "no"))
    await async_wait_until(logging_reject.is_set)
    hold.set()
    assert await sweep == 0
    release_reject.set()
    await answering
    assert _sent(rt) == ["no"], "the person's reject stands"


@pytest.mark.asyncio
async def test_a_long_step_keeps_naming_its_calls_past_the_cache_bound():
    # Settled calls release their rows at their terminal frame, as on a chat
    # session, so the bound counts calls in flight, not the turn's length.
    router, rt, sink, _, _ = await _real_identity_router()
    for i in range(300):
        call = dict(_SHELL_FRAME, toolCallId=f"tc-done-{i}", rawInput={"command": "true"})
        sink.on_watched_notification("session/update", _frame(call))
        done = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": f"tc-done-{i}",
            "status": "completed",
        }
        sink.on_watched_notification("session/update", _frame(done))
    await _ask_framed(sink, 1, _SHELL_FRAME, _SHELL_REQ)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["yes"], "the 301st call still has its identity"


@pytest.mark.asyncio
async def test_a_frame_after_the_request_cannot_change_its_identity():
    # The identity is read when the request arrives, in frame order: a later
    # frame for the same session and call id (here one that would make the call
    # a harmless read) cannot rewrite what admission judges.
    router, rt, sink, _, _ = await _real_identity_router("trust_reads")
    edit = dict(_EDIT_FRAME, toolCallId="tc-x")
    req = dict(_EDIT_REQ, toolCall=dict(_EDIT_REQ["toolCall"], toolCallId="tc-x"))
    sink.on_watched_notification("session/update", _frame(edit))
    sink.on_step_request(1, "s0", PERM, req)
    late = dict(_SHELL_FRAME, toolCallId="tc-x", rawInput={"command": "ls"})
    sink.on_watched_notification("session/update", _frame(late))
    await async_wait_until(lambda: router.approvals() or rt.responses)
    assert _sent(rt) == [], "judged as the write it was: held for a person"
    assert len(router.approvals()) == 1


# ── Every shown string: neutralized, redacted over a bounded window, then capped ──

_KEY = "sk-" + "proj-" + "a1B2c3D4e5" * 4


def _straddling(cap: int) -> str:
    """Text whose secret starts just before *cap*, so a cut-then-redact leaks its prefix."""
    return "x" * (cap - 10) + " " + _KEY


def _leaks(text: Any) -> bool:
    return _KEY[:8] in json.dumps(text)


@pytest.mark.asyncio
async def test_a_held_steps_title_is_redacted_before_its_cap():
    from kiro_crew.kas_workflows.router_base import _MAX_DISPLAY_CHARS

    router, rt, sink, _, _ = await _router("normal")
    params = _shell()
    params["toolCall"] = dict(params["toolCall"], title=_straddling(_MAX_DISPLAY_CHARS))
    sink.on_step_request(1, "s0", PERM, params)
    await async_wait_until(lambda: router.approvals())
    [card] = router.approvals()
    assert not _leaks(card["title"])


@pytest.mark.asyncio
async def test_a_held_steps_option_names_are_redacted_before_their_cap():
    from kiro_crew.kas_workflows.router_base import _MAX_OPTION_NAME

    router, rt, sink, _, _ = await _router("normal")
    params = _shell()
    params["options"] = [dict(o, name=_straddling(_MAX_OPTION_NAME)) for o in _OPTIONS]
    sink.on_step_request(1, "s0", PERM, params)
    await async_wait_until(lambda: router.approvals())
    [card] = router.approvals()
    assert not _leaks([o["name"] for o in card["options"]])


@pytest.mark.asyncio
async def test_the_picked_option_id_is_redacted_in_the_security_log():
    router, rt, sink, _, audits = await _router("normal")
    params = _shell()
    secret_id = _straddling(200)
    params["options"] = [dict(_OPTIONS[0], optionId=secret_id), _OPTIONS[2]]
    sink.on_step_request(1, "s0", PERM, params)
    await async_wait_until(lambda: router.approvals())
    await router.answer(router.approvals()[0]["key"], secret_id)
    [person] = [a for a in audits if a["metadata"].get("reason") == "person"]
    assert not _leaks(person["metadata"]["optionId"])


def _question_kwargs() -> dict[str, Any]:
    return dict(
        key="k",
        request_id=1,
        session_id="s0",
        owner=("w0", "a"),
        created_at=1.0,
        runtime=None,
        path=None,
        slot=None,
    )


@pytest.mark.parametrize(
    "site", ("bridge-title", "bridge-option", "user-input-question", "user-input-answer")
)
def test_a_questions_shown_strings_are_redacted_before_their_cap(site):
    from kiro_crew.kas_workflows import asks

    if site == "bridge-title":
        params = {
            "toolCall": {"title": _straddling(asks._MAX_PROMPT_CHARS)},
            "options": [{"optionId": "a", "kind": "allow_once", "name": "Yes"}],
        }
        shown = asks.question_from_request(params, **_question_kwargs()).prompt
    elif site == "bridge-option":
        params = {
            "toolCall": {"title": "Pick"},
            "options": [
                {"optionId": "a", "kind": "allow_once", "name": _straddling(asks._MAX_LABEL_CHARS)}
            ],
        }
        shown = [o.label for o in asks.question_from_request(params, **_question_kwargs()).options]
    elif site == "user-input-question":
        params = {"question": _straddling(asks._MAX_PROMPT_CHARS), "options": [{"title": "Yes"}]}
        shown = asks.question_from_user_input(params, **_question_kwargs()).prompt
    else:
        question = asks.question_from_user_input(
            {"question": "Q?", "options": []}, **_question_kwargs()
        )
        shown = question.display_for(_straddling(asks._MAX_ANSWER_CHARS))
    assert not _leaks(shown), site


@pytest.mark.asyncio
async def test_the_sweep_never_restores_a_card_its_session_withdrew():
    import asyncio
    import threading

    logging = threading.Event()
    release = threading.Event()

    def _audit(**kw):
        if kw.get("outcome") == "auto_approved":
            logging.set()
            release.wait(30)
            raise OSError("log down")  # the auto-approve then fails

    router, rt, sink, mode, _ = await _router("normal", audit=_audit)
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    mode[0] = "trust"
    sweep = asyncio.ensure_future(router.recheck_pending())
    await async_wait_until(logging.is_set)
    await stop_step(router, "s0")  # the step's turn was stopped meanwhile
    release.set()
    assert await sweep == 0
    assert router.approvals() == [], "the withdrawn request's card is not restored"
    assert [r["outcome"]["outcome"] for _i, r in rt.responses] == ["cancelled"]


@pytest.mark.parametrize("site", ("trust", "grant-use", "mode-sweep", "refusal", "person-answer"))
@pytest.mark.asyncio
async def test_every_audit_of_a_step_redacts_its_title_and_node_before_their_caps(
    monkeypatch, site
):
    from kiro_crew.kas_workflows.router_base import _MAX_DISPLAY_CHARS

    # No command, so the audit names the title; a node id a recipe wrote.
    title = _straddling(_MAX_DISPLAY_CHARS)
    # An argument-free MCP call: no command, and grantable "Always".
    params = {
        "sessionId": "s0",
        "toolCall": {"toolCallId": "tc-t", "title": title, "rawInput": {}},
        "options": list(_OPTIONS),
        "_meta": {
            "kiro": {"toolId": "mcp_probe_echo", "mcpServerName": "probe", "toolName": "echo"}
        },
    }
    rules = _Rules(monkeypatch, "gate-deny")
    start = "trust" if site == "trust" else "normal"
    router, rt, sink, mode, audits = await _router(start)
    node = _straddling(200)
    router.mirror.run_for_step_session = lambda _s: ("w0", node)
    if site == "grant-use":
        sink.on_step_request(9, "s0", PERM, params)
        await async_wait_until(lambda: router.approvals())
        await router.answer(router.approvals()[0]["key"], "always")
    if site == "refusal":
        rules.on = True
    sink.on_step_request(1, "s0", PERM, dict(params, toolCall=dict(params["toolCall"])))
    if site == "mode-sweep":
        await async_wait_until(lambda: router.approvals())
        mode[0] = "trust"
        await router.recheck_pending()
    if site == "person-answer":
        await async_wait_until(lambda: router.approvals())
        await router.answer(router.approvals()[0]["key"], "yes")
    await async_wait_until(lambda: len(audits) >= (2 if site == "grant-use" else 1))
    assert not _leaks([(a["tool_name"], a["metadata"].get("nodeId")) for a in audits]), site


@pytest.mark.asyncio
async def test_a_held_step_retains_nothing_past_its_bounds():
    # An oversized option name: the held card keeps a capped copy, and the kept
    # identity event keeps no options at all.
    from kiro_crew.kas_workflows.router_base import _MAX_OPTION_NAME

    router, rt, sink, _, _ = await _real_identity_router("normal")
    huge = "n" * 1_000_000
    req = dict(_SHELL_REQ, options=[dict(o, name=huge) for o in _OPTIONS])
    await _ask_framed(sink, 1, _SHELL_FRAME, req)
    await async_wait_until(lambda: router.approvals())
    [held] = router._pending.values()
    assert held.gate_event is not None and held.gate_event.options == []
    assert all(len(o["name"]) <= _MAX_OPTION_NAME for o in held.options)
    retained = json.dumps(
        [held.title, held.raw_title, held.command, list(held.options), held.raw_params], default=str
    )
    assert len(retained) < 200_000, "nothing near the option name's size is kept"


def test_an_identity_event_whose_title_is_over_the_bound_is_not_kept():
    # Not kept means not re-checkable: the sweep and a person's allow refuse it
    # ("identity: the step's identity was not kept").
    from kiro_crew.kas_workflows.router_gate import MAX_RETAINED_TITLE_CHARS, _retained_event
    from kiro_crew.kas_workflows.step_identity import StepToolCalls

    calls = StepToolCalls()
    calls.observe("s0", _SHELL_FRAME)
    event, why = calls.event_for(_SHELL_REQ)
    assert why is None and _retained_event(event) is not None
    event.title = "t" * (MAX_RETAINED_TITLE_CHARS + 1)
    assert _retained_event(event) is None


@pytest.mark.parametrize("field", ("title", "tool_kind", "mcp_identity_unreadable_reason"))
def test_an_identity_event_with_any_oversized_field_is_not_kept(field):
    from kiro_crew.kas_workflows.router_gate import MAX_RETAINED_TITLE_CHARS, _retained_event
    from kiro_crew.kas_workflows.step_identity import StepToolCalls

    calls = StepToolCalls()
    calls.observe("s0", _SHELL_FRAME)
    event, why = calls.event_for(_SHELL_REQ)
    assert why is None and _retained_event(event) is not None
    setattr(event, field, "k" * (MAX_RETAINED_TITLE_CHARS + 1))
    assert _retained_event(event) is None


def test_a_deny_rule_refusal_logs_the_node_id_redacted_and_capped(caplog):
    import asyncio

    from kas_workflow_helpers import make_router as _make

    from kiro_crew.kas_workflows import router_answers

    router = _make(screen=lambda _c: "denied")
    node = "n" * 50_000 + "\nforged line"
    router.mirror.run_for_step_session = lambda _s: ("w0", node)
    rt = FakeRuntime()
    claim = router_answers._register(router, lambda: rt, 1, "s0", PERM)
    with caplog.at_level("WARNING", logger="kiro_crew.kas_workflows.router"):
        asyncio.run(router._admit(claim, _shell("rm x")))
    [line] = [r.getMessage() for r in caplog.records if "deny rules" in r.getMessage()]
    assert len(line) < 1_000 and "forged line" not in line


def test_a_deny_rule_refusal_log_line_cannot_be_forged_by_a_node_id(caplog):
    import asyncio

    from kas_workflow_helpers import make_router as _make

    from kiro_crew.kas_workflows import router_answers

    router = _make(screen=lambda _c: "denied")
    router.mirror.run_for_step_session = lambda _s: ("w0", "review\n12:00 INFO forged\u202e")
    rt = FakeRuntime()
    claim = router_answers._register(router, lambda: rt, 1, "s0", PERM)
    with caplog.at_level("WARNING", logger="kiro_crew.kas_workflows.router"):
        asyncio.run(router._admit(claim, _shell("rm x")))
    [line] = [r.getMessage() for r in caplog.records if "deny rules" in r.getMessage()]
    assert "\n" not in line and "\u202e" not in line


@pytest.mark.asyncio
async def test_a_held_steps_title_is_shown_without_control_or_bidi_characters():
    router, rt, sink, _, _ = await _router("normal")
    params = _shell()
    params["toolCall"] = dict(params["toolCall"], title="Run ls\r\nApproved by admin\u202e")
    sink.on_step_request(1, "s0", PERM, params)
    await async_wait_until(lambda: router.approvals())
    [card] = router.approvals()
    assert not any(c in card["title"] for c in "\r\n\u202e")


@pytest.mark.asyncio
async def test_a_step_with_no_title_is_refused_under_trust():
    # As the chat turn refuses a call that carries no title.
    router, rt, sink, _, audits = await _real_identity_router("trust")
    frame = dict(_SHELL_FRAME, title="")
    req = dict(_SHELL_REQ, toolCall=dict(_SHELL_REQ["toolCall"], title=""))
    await _ask_framed(sink, 1, frame, req)
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"]


@pytest.mark.parametrize("mode", ("trust", "trust_reads"))
@pytest.mark.asyncio
async def test_an_organizations_web_ask_rule_holds_a_trusted_step_for_a_person(monkeypatch, mode):
    # As a chat turn holds it (web_ask_rule.needs_person): no approval mode answers it.
    import types

    from kiro_crew import web_ask_rule

    # LTS for the web rule alone: the rest of the process keeps its own edition.
    monkeypatch.setattr(web_ask_rule, "edition", types.SimpleNamespace(is_lts=lambda: True))
    monkeypatch.setattr(web_ask_rule, "asked_terms", lambda: frozenset({"web_fetch"}))
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-web",
        "title": "Fetch URL",
        "kind": "fetch",
        "status": "pending",
        "rawInput": {"url": "https://example.com"},
        "_meta": {"kiro": {"toolOrigin": "default"}},
    }
    req = {
        "sessionId": "s0",
        "toolCall": {"toolCallId": "tc-web", "status": "pending", "title": "Fetch URL"},
        "options": list(_OPTIONS),
        "_meta": {"kiro": {"toolId": "web_fetch"}},
    }
    router, rt, sink, _, _ = await _real_identity_router(mode)
    await _ask_framed(sink, 1, frame, req)
    await async_wait_until(lambda: router.approvals() or rt.responses)
    assert _sent(rt) == [] and len(router.approvals()) == 1


# ── A Stop that lands while a step's request is being judged: one answer ──


@pytest.mark.parametrize("mode", ("normal", "trust"))
@pytest.mark.asyncio
async def test_a_stop_during_admission_answers_cancelled_once_and_holds_no_card(mode):
    import threading

    judging = threading.Event()
    release = threading.Event()

    def _input_screen(_event, _params):
        judging.set()
        release.wait(30)
        return None

    router, rt, sink, _, audits = await _router(mode, input_screen=_input_screen)
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(judging.is_set)
    await stop_step(router, "s0")  # the step was stopped meanwhile
    release.set()
    await async_wait_until(lambda: not router._tasks)
    assert [r["outcome"]["outcome"] for _i, r in rt.responses] == ["cancelled"], "one answer"
    assert router.approvals() == [], "no card for a stopped step"
    assert [a for a in audits if a["outcome"] == "auto_approved"] == []


@pytest.mark.asyncio
async def test_a_stop_during_the_sweeps_audit_answers_once():
    import threading

    logging = threading.Event()
    release = threading.Event()

    def _audit(**kw):
        if kw.get("outcome") == "auto_approved":
            logging.set()
            release.wait(30)

    router, rt, sink, mode, _ = await _router("normal", audit=_audit)
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: router.approvals())
    mode[0] = "trust"
    import asyncio

    sweep = asyncio.ensure_future(router.recheck_pending())
    await async_wait_until(logging.is_set)
    await stop_step(router, "s0")
    release.set()
    assert await sweep == 0
    assert [r["outcome"]["outcome"] for _i, r in rt.responses] == ["cancelled"], "one answer"
    assert router.approvals() == []


@pytest.mark.parametrize("sep", ("\u2028", "\u2029"))
def test_a_line_separator_cannot_forge_a_line(sep):
    from kiro_crew.kas_workflows import asks
    from kiro_crew.kas_workflows import router as router_mod

    assert sep not in router_mod._rid("review" + sep + "forged")
    assert sep not in (asks._shown("a" + sep + "b") or "")


# ── After a Stop: no allow reaches the engine, one answer each, each logged ──


class _LockedRuntime(FakeRuntime):
    """A runtime whose writes share one stdin lock, as ``AcpRuntime``'s do."""

    def __init__(self) -> None:
        super().__init__()
        import asyncio

        self.lock = asyncio.Lock()
        self.waiting = 0
        self.wire: list[str] = []

    async def send_response(self, request_id, result, *, withdrawal_check=None):
        self.waiting += 1
        async with self.lock:
            self.waiting -= 1
            if withdrawal_check is not None:
                withdrawal_check()  # under the lock, in the write's critical section
            self.responses.append((request_id, result))
            self.wire.append(f"answer:{result['outcome']['outcome']}")

    async def send_notification(self, method, params):
        async with self.lock:
            self.notifications.append((method, params))
            self.wire.append(method)


async def _locked_router(mode: str):
    audits: list[dict[str, Any]] = []
    router = make_router(audit=lambda **kw: audits.append(kw))
    await router.ensure_ledger()
    router._ledger["w0"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0}
    router.note_chat_session("chat-1", None, mode=lambda: (mode, mode))
    rt = _LockedRuntime()
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    return router, rt, sink, audits


def _cancels(audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in audits if a["outcome"] == "rejected_on_cancel"]


@pytest.mark.asyncio
async def test_an_allow_queued_behind_a_stop_is_never_written():
    import asyncio

    router, rt, sink, audits = await _locked_router("trust")
    await rt.lock.acquire()  # another session's long frame holds the pipe
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(lambda: rt.waiting == 1)  # the allow waits on the lock
    router._mark_stopped("s0")  # Stop, before its session/cancel is queued
    cancel = asyncio.ensure_future(rt.send_notification("session/cancel", {"sessionId": "s0"}))
    withdraw = asyncio.ensure_future(router._withdraw_session_asks("s0"))
    rt.lock.release()
    await cancel
    await withdraw
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["session/cancel", "answer:cancelled"], "no allow after the Stop"
    assert len(_cancels(audits)) == 1


@pytest.mark.asyncio
async def test_a_request_read_after_the_stop_is_answered_cancelled_once():
    router, rt, sink, audits = await _locked_router("trust")
    await stop_step(router, "s0")  # stopped, nothing open yet
    sink.on_step_request(2, "s0", PERM, _shell())  # its frame was already on the pipe
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["answer:cancelled"] and router.approvals() == []
    [row] = _cancels(audits)
    assert row["metadata"]["reason"] == "asked_after_stop"


@pytest.mark.asyncio
async def test_an_admission_not_yet_started_when_the_step_is_stopped_answers_once():
    router, rt, sink, audits = await _locked_router("trust")
    sink.on_step_request(3, "s0", PERM, _shell())  # its task has not run yet
    router._mark_stopped("s0")
    await stop_step(router, "s0")
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["answer:cancelled"] and router.approvals() == []
    assert len(_cancels(audits)) == 1


@pytest.mark.asyncio
async def test_every_ask_a_stop_cancels_is_logged():
    router, rt, sink, audits = await _locked_router("normal")
    sink.on_step_request(4, "s0", PERM, _shell("ls"))
    sink.on_step_request(5, "s0", PERM, _shell("pwd"))
    await async_wait_until(lambda: len(router.approvals()) == 2)
    await stop_step(router, "s0")
    assert rt.wire == ["answer:cancelled", "answer:cancelled"]
    assert sorted(a["request_id"] for a in _cancels(audits)) == ["4", "5"]


def _turn_start(sink) -> None:
    sink.on_watched_notification(
        "session/update",
        {
            "sessionId": "s0",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": ""},
                "_meta": {"kiro": {"kind": "turn_start"}},
            },
        },
    )


@pytest.mark.asyncio
async def test_a_buffered_turn_start_after_the_stop_does_not_reopen_the_step():
    router, rt, sink, _audits = await _locked_router("trust")
    await stop_step(router, "s0")  # Stopped
    _turn_start(sink)  # the stopped turn's own frame, read after the Stop
    sink.on_step_request(6, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["answer:cancelled"], "no allow after the Stop"


@pytest.mark.asyncio
async def test_resume_from_crew_reopens_a_stopped_step():
    router, rt, sink, _audits = await _locked_router("trust")
    await stop_step(router, "s0")
    await router.control("w0", "resume")
    sink.on_step_request(7, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire and rt.wire[-1] != "answer:cancelled"


@pytest.mark.asyncio
async def test_an_over_long_request_id_is_not_kept_in_answer_history():
    router, _rt, sink, _audits = await _locked_router("trust")
    sink.on_step_request("x" * 100_000, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    assert _rt.responses, "it was answered"
    assert not router._claims, "nothing is kept once it is answered"


@pytest.mark.asyncio
async def test_a_stopped_admission_racing_the_withdrawal_logs_one_row():
    import threading

    router, rt, sink, audits = await _locked_router("trust")
    done = threading.Event()

    def slow_audit(**kw) -> None:
        if kw["outcome"] == "rejected_on_cancel" and kw["metadata"]["reason"] == "step_stopped":
            done.wait(30)  # the admission task runs while this row is written
        audits.append(kw)

    router._audit = slow_audit
    before = set(router._tasks)
    sink.on_step_request(8, "s0", PERM, _shell())  # its task has not run yet
    [task] = list(set(router._tasks) - before)
    task.add_done_callback(lambda _t: done.set())
    await stop_step(router, "s0")
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["answer:cancelled"]
    assert len(_cancels(audits)) == 1


@pytest.mark.asyncio
async def test_a_resume_during_the_stop_never_frees_an_allow_already_queued():
    import asyncio

    router, rt, sink, _audits = await _locked_router("trust")
    await rt.lock.acquire()
    sink.on_step_request(9, "s0", PERM, _shell())
    await async_wait_until(lambda: rt.waiting == 1)  # the allow waits on the lock
    router._mark_stopped("s0")
    withdraw = asyncio.ensure_future(router._withdraw_session_asks("s0"))
    await async_wait_until(lambda: any(c.cancelling for c in router._claims.values()))
    router._end_stops(router._stops_of_run("w0"))  # a Resume taken before the cancel
    rt.lock.release()
    await withdraw
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["answer:cancelled"], "the request open at the Stop is never allowed"


@pytest.mark.asyncio
async def test_a_refused_resume_keeps_the_stop():
    router, rt, sink, _audits = await _locked_router("trust")
    await stop_step(router, "s0")
    from kiro_crew.kas_workflows.router import KasWorkflowError

    rt.answers[wire.METHOD_RESUME] = RuntimeError("refused")
    with pytest.raises(KasWorkflowError):
        await router.control("w0", "resume")
    sink.on_step_request(10, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    assert rt.wire == ["answer:cancelled"], "still stopped: no allow"


class _HeldRuntime(_LockedRuntime):
    """Kiro Agent takes a re-drive but its reply waits until ``release``."""

    def __init__(self, held: str) -> None:
        import asyncio

        super().__init__()
        self.held = held
        self.release = asyncio.Event()

    async def extension_request(self, method, params, *, timeout=30.0):
        self.calls.append((method, params))
        if method == self.held:
            await self.release.wait()
        return {}


_REDRIVES = {
    "resume": (wire.METHOD_RESUME, lambda r: r.control("w0", "resume")),
    "retry": (wire.METHOD_RETRY, lambda r: r.retry("w0")),
    "restart": (wire.METHOD_INVOKE, lambda r: r.restart("w0")),
    "grant": (wire.METHOD_RESUME, lambda r: r.grant("w0", "a", 2)),
    "reply": ("session/prompt", lambda r: r.reply_to_step("w0", "a", None, "go on")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("redrive", sorted(_REDRIVES))
async def test_a_redrive_finishing_after_a_newer_stop_keeps_that_stop(redrive, monkeypatch):
    import asyncio

    method, call = _REDRIVES[redrive]
    audits: list[dict[str, Any]] = []
    router = make_router(audit=lambda **kw: audits.append(kw))
    await router.ensure_ledger()
    router._ledger["w0"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0}
    router.note_chat_session("chat-1", None, mode=lambda: ("trust", "trust"))
    rt = _HeldRuntime(method)
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    monkeypatch.setattr(router.mirror, "status", lambda _w: wire.STATUS_FAILED)
    monkeypatch.setattr(router.mirror, "step_awaits_reply", lambda _w, _s: True)
    await stop_step(router, "s0")  # the Stop the re-drive is sent over
    task = asyncio.ensure_future(call(router))
    await async_wait_until(lambda: any(m == method for m, _p in rt.calls))
    await stop_step(router, "s0")  # a newer Stop, while it awaits Kiro Agent
    rt.release.set()
    await task
    assert "s0" in router._stopped, "a re-drive never ends a Stop newer than itself"
    sink.on_step_request(11, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    assert [r for i, r in rt.responses if i == 11] == [{"outcome": {"outcome": "cancelled"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("redrive", sorted(_REDRIVES))
async def test_a_redrive_ends_the_stop_it_was_sent_over(redrive, monkeypatch):
    method, call = _REDRIVES[redrive]
    router, _rt, sink, _audits = await _locked_router("trust")
    monkeypatch.setattr(router.mirror, "status", lambda _w: wire.STATUS_FAILED)
    monkeypatch.setattr(router.mirror, "step_awaits_reply", lambda _w, _s: True)
    await stop_step(router, "s0")
    await call(router)
    assert "s0" not in router._stopped


def _held_audit(audits: list[dict[str, Any]], outcome: str):
    """An audit that holds the first *outcome* row until the returned event is set."""
    import threading

    release, reached = threading.Event(), threading.Event()

    def audit(**kw) -> None:
        if kw["outcome"] == outcome and not reached.is_set():
            reached.set()
            release.wait(30)
        audits.append(kw)

    return audit, release, reached


async def _answer_many_later(router, sink, rt, count: int = 4096) -> None:
    """*count* later requests on the stopped step, each answered (cancelled) once."""
    before = len(rt.responses)
    for n in range(count):
        sink.on_step_request(10_000 + n, "s0", PERM, _shell())
    await async_wait_until(lambda: len(rt.responses) >= before + count)


def _answers_to(rt, request_id) -> list[str]:
    return [r["outcome"]["outcome"] for i, r in rt.responses if i == request_id]


@pytest.mark.asyncio
async def test_an_auto_approval_paused_on_its_log_write_stays_unsent_after_a_stop():
    audits: list[dict[str, Any]] = []
    audit, release, reached = _held_audit(audits, "auto_approved")
    router, rt, sink, _a = await _locked_router("trust")
    router._audit = audit
    sink.on_step_request(1, "s0", PERM, _shell())
    await async_wait_until(reached.is_set)  # its approval row is being written
    await stop_step(router, "s0")
    await _answer_many_later(router, sink, rt)
    release.set()
    await async_wait_until(lambda: not router._tasks)
    assert _answers_to(rt, 1) == ["cancelled"], "one answer, and no allow after the Stop"


@pytest.mark.asyncio
async def test_a_person_allow_paused_on_its_log_write_stays_unsent_after_a_stop():
    import asyncio

    from kiro_crew.kas_workflows.router import KasWorkflowError

    audits: list[dict[str, Any]] = []
    audit, release, reached = _held_audit(audits, "approved")
    router, rt, sink, _a = await _locked_router("normal")
    router._audit = audit
    sink.on_step_request(2, "s0", PERM, _shell("ls"))
    await async_wait_until(lambda: len(router.approvals()) == 1)
    [card] = router.approvals()
    answer = asyncio.ensure_future(router.answer(card["key"], "yes"))
    await async_wait_until(reached.is_set)  # the person's allow row is being written
    await stop_step(router, "s0")
    await _answer_many_later(router, sink, rt)
    release.set()
    with pytest.raises(KasWorkflowError):
        await answer
    assert _answers_to(rt, 2) == ["cancelled"], "one answer, and no allow after the Stop"


class _SlowCancelRuntime(_LockedRuntime):
    """Kiro Agent's stdin is slow to take the Stop's ``session/cancel``."""

    def __init__(self) -> None:
        import asyncio

        super().__init__()
        self.cancel_taken = asyncio.Event()
        self.cancel_waiting = False

    async def send_notification(self, method, params):
        self.cancel_waiting = True
        await self.cancel_taken.wait()
        await super().send_notification(method, params)

    async def extension_request(self, method, params, *, timeout=30.0):
        self.calls.append((method, params))
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("redrive", sorted(_REDRIVES))
async def test_a_redrive_sent_while_the_stop_is_written_ends_that_stop(redrive, monkeypatch):
    import asyncio

    _method, call = _REDRIVES[redrive]
    router = make_router(audit=lambda **_kw: None)
    await router.ensure_ledger()
    router._ledger["w0"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0}
    router.note_chat_session("chat-1", None, mode=lambda: ("trust", "trust"))
    rt = _SlowCancelRuntime()
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    stop = asyncio.ensure_future(router.cancel_step("w0", "a"))
    await async_wait_until(lambda: rt.cancel_waiting)
    monkeypatch.setattr(router.mirror, "status", lambda _w: wire.STATUS_FAILED)
    monkeypatch.setattr(router.mirror, "step_awaits_reply", lambda _w, _s: True)
    await call(router)  # sent after the Stop, while its cancel is still being written
    rt.cancel_taken.set()
    await stop
    assert "s0" not in router._stopped, "the Stop is marked once, so this re-drive ends it"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_id", ["x" * 100_000, "a\nforged", "id\u202e"], ids=["long", "newline", "bidi"]
)
async def test_a_request_id_reaches_the_security_log_only_in_a_safe_shape(request_id):
    router, _rt, sink, audits = await _locked_router("trust")
    sink.on_step_request(request_id, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    await stop_step(router, "s0")
    sink.on_step_request(request_id + "-late", "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    logged = [a["request_id"] for a in audits]
    assert len(logged) == 2
    assert all(r.startswith("sha256:") and len(r) == 71 for r in logged)


@pytest.mark.asyncio
async def test_a_request_under_a_dead_runtimes_claim_key_gets_a_fresh_claim_and_an_answer():
    import weakref

    from kiro_crew.kas_workflows import router_answers

    class _Gone:
        pass

    router, rt, sink, _audits = await _locked_router("trust")
    gone = _Gone()
    dead = weakref.ref(gone)
    del gone
    key = router_answers._key(weakref.ref(rt), 61)
    stale = router_answers.AnswerClaim("s0", 61, PERM, dead, router._stopped)
    stale.key = key
    router._claims[key] = stale  # an entry this runtime's request collides with
    sink.on_step_request(61, "s0", PERM, _shell())
    await async_wait_until(lambda: not router._tasks)
    assert [i for i, _r in rt.responses] == [61], "the replacement request is answered"


def test_a_claim_key_is_never_reused_by_a_later_runtime_at_the_same_address(monkeypatch):
    import weakref

    from kiro_crew.kas_workflows import router_answers

    # Every runtime at one address: what CPython does once the first is collected.
    monkeypatch.setattr(router_answers, "id", lambda _obj: 4096, raising=False)
    first, later = FakeRuntime(), FakeRuntime()
    old_key = router_answers._key(weakref.ref(first), 7)
    assert router_answers._key(weakref.ref(later), 7) != old_key
