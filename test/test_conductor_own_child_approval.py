"""A conductor's ``session_send`` / ``session_close`` into a session it created needs no
per-call approval (rfc-conductor-own-child-verbs.md, section 5.2).

Only ``kirocrew-conductor`` and ``kirocrew-team-lead`` qualify, only for those two
verbs, only with a verified ``kirocrew-dashboard`` identity and arguments, and only
when the ``target`` is the exact key of a live session whose GATEWAY-VOUCHED
creator is the caller. ``agent.conductor_own_child_verbs`` turns it off. Everything
else asks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import crew_mode
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard import chat_runner  # noqa: F401  (composes tool_approval's names)
from kiro_crew.dashboard.chat_turn import tool_approval
from kiro_crew.dashboard.chat_turn.tool_approval import (
    _conductor_own_child_call,
    _conductor_own_child_verbs_enabled,
)
from kiro_crew.hook_runtime.hook_types import HooksConfig

_RULE_NS = _conductor_own_child_call.__globals__


@pytest.fixture(autouse=True)
def _defaults(_floor_monkeypatch):
    # The crew log off: no session tree, so the birth stamp with its mint witness decides.
    _floor_monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    # The rule does not wait for the Crew panel's routing switch (off on main).
    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", False)
    # The runner composes this module's functions into its own namespace, so the
    # switch is replaced where the rule looks it up.
    _floor_monkeypatch.setitem(_RULE_NS, "_conductor_own_child_verbs_enabled", lambda: True)


def _slot(key: str, *, agent: str = "default", created_by: str = "", minted: bool = True):
    return SimpleNamespace(
        key=key,
        agent=agent,
        _created_by=created_by,
        _lineage_minted=minted,
        _trust=False,
        _trust_reads=False,
    )


def _builder(**hooks: object) -> SimpleNamespace:
    """A context builder whose hooks config is *hooks* (the gate itself is not run)."""
    return SimpleNamespace(hooks=SimpleNamespace(_config=HooksConfig(**hooks)))


def _state(**hooks: object) -> SimpleNamespace:
    return SimpleNamespace(
        context_builder=_builder(**hooks),
        _slots={
            "cond": _slot("cond", agent="kirocrew-conductor"),
            "lead": _slot("lead", agent="kirocrew-team-lead"),
            "ledger": _slot("ledger", agent="kirocrew-ledger-conductor"),
            "plain": _slot("plain", agent="kirocrew"),
            "w-cond": _slot("w-cond", agent="kirocrew-worker", created_by="cond"),
            "w-lead": _slot("w-lead", agent="kirocrew-worker", created_by="lead"),
            "w-ledger": _slot("w-ledger", agent="kirocrew-worker", created_by="ledger"),
            "w-plain": _slot("w-plain", agent="kirocrew-worker", created_by="plain"),
            "tab": _slot("tab"),
            # A birth stamp this process never minted (read back from saved metadata).
            "restored": _slot("restored", agent="kirocrew-worker", created_by="cond", minted=False),
        },
    )


def _event(tool: str = "session_send", target: object = "w-cond", **over: object):
    fields = dict(
        sub_session_id="",
        mcp_identity_trusted=True,
        raw_params_trusted=True,
        mcp_server_name="kirocrew-dashboard",
        tool_name=tool,
        raw_tool_params={"target": target, "message": "go"},
    )
    fields.update(over)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("tool", ["session_send", "session_close"])
@pytest.mark.parametrize("caller, child", [("cond", "w-cond"), ("lead", "w-lead")])
def test_a_send_or_close_to_its_own_child_is_granted(tool, caller, child) -> None:
    state = _state()
    assert _conductor_own_child_call(state, state._slots[caller], _event(tool, child)) is True


def test_a_deferred_tool_call_reads_the_target_from_its_arguments() -> None:
    state = _state()
    wrapped = {"tool_id": "kirocrew-dashboard::session_send", "arguments": {"target": "w-cond"}}
    assert _conductor_own_child_call(state, state._slots["cond"], _event(raw_tool_params=wrapped))


def test_a_codex_envelope_reads_the_target_from_its_arguments() -> None:
    state = _state()
    cond = state._slots["cond"]
    codex = {
        "server": "kirocrew-dashboard",
        "tool": "session_send",
        "arguments": {"target": "w-cond"},
    }
    assert _conductor_own_child_call(state, cond, _event(raw_tool_params=codex)) is True
    # An envelope naming another tool is not opened.
    other = {**codex, "tool": "session_stop"}
    assert _conductor_own_child_call(state, cond, _event(raw_tool_params=other)) is False


@pytest.mark.parametrize(
    "caller, event",
    [
        ("cond", _event(target="w-lead")),  # a sibling conductor's worker
        ("cond", _event(target="tab")),  # a person's tab
        ("cond", _event(target="cond")),  # itself
        ("cond", _event(target="Worker title")),  # by title, not key
        ("cond", _event(target="restored")),  # creator not vouched this process
        ("cond", _event(tool="session_close", target="tab")),
        ("cond", _event(tool="work_dispatch")),  # still prompts (rfc-work-dispatch Q1)
        ("cond", _event(tool="session_stop")),
        ("cond", _event(tool="session_broadcast")),
        ("cond", _event(mcp_server_name="kirocrew-core")),
        ("cond", _event(mcp_identity_trusted=False)),
        ("cond", _event(raw_params_trusted=False)),
        ("cond", _event(sub_session_id="sub-1")),  # a subagent's call
        ("cond", _event(native_child_call=True)),  # a native KAS sub-agent's call
        ("ledger", _event(target="w-ledger")),  # not a named agent
        ("plain", _event(target="w-plain")),
        ("ledger", _event(tool="session_close", target="w-ledger")),
    ],
)
def test_anything_else_still_asks(caller: str, event: SimpleNamespace) -> None:
    state = _state()
    assert _conductor_own_child_call(state, state._slots[caller], event) is False


@pytest.mark.parametrize(
    "pattern", ["@kirocrew-dashboard/session_*", "@kirocrew-dashboard", "*session_send*"]
)
def test_an_operator_confirm_pattern_still_asks(pattern: str) -> None:
    state = _state(confirm_tools=[pattern])
    assert _conductor_own_child_call(state, state._slots["cond"], _event()) is False


@pytest.mark.parametrize(
    "pattern, title",
    [
        ("session_send", ""),  # a bare tool-name pattern
        ("mcp__kirocrew-dashboard__session_send", ""),
        ("Sending to worker*", "Sending to worker w-cond"),  # keyed on the title
    ],
)
def test_a_confirm_pattern_in_any_spelling_the_gate_reads_still_asks(pattern, title) -> None:
    state = _state(confirm_tools=[pattern])
    event = _event(title=title)
    assert _conductor_own_child_call(state, state._slots["cond"], event) is False


def test_an_unrelated_confirm_pattern_does_not_ask() -> None:
    state = _state(confirm_tools=["@kirocrew-core/*"])
    assert _conductor_own_child_call(state, state._slots["cond"], _event()) is True


def test_an_unreadable_confirm_list_or_no_hooks_config_asks() -> None:
    state = _state(confirm_tools_unknown=True)
    assert _conductor_own_child_call(state, state._slots["cond"], _event()) is False
    state = _state()
    state.context_builder = None
    assert _conductor_own_child_call(state, state._slots["cond"], _event()) is False


def test_a_settled_tree_edge_decides_not_the_stamp(monkeypatch) -> None:
    state = _state()
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    monkeypatch.setattr(
        crew_mode, "persisted_tree_slots", lambda: frozenset({"restored", "w-cond"})
    )
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"restored": "cond"})
    cond = state._slots["cond"]
    # After a restart the crew log's settled edge vouches for the child.
    assert _conductor_own_child_call(state, cond, _event(target="restored")) is True
    # A released child (in the tree, no parent) is no one's, whatever its stamp says.
    assert _conductor_own_child_call(state, cond, _event(target="w-cond")) is False


def test_the_switch_off_asks_for_every_case(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setitem(_RULE_NS, "_conductor_own_child_verbs_enabled", lambda: False)
    state = _state()
    for caller, child in (("cond", "w-cond"), ("lead", "w-lead")):
        for tool in ("session_send", "session_close"):
            assert not _conductor_own_child_call(state, state._slots[caller], _event(tool, child))


def test_the_call_leaves_the_child_mode_alone() -> None:
    state = _state()
    child = state._slots["w-cond"]
    before = (child._trust, child._trust_reads)
    assert _conductor_own_child_call(state, state._slots["cond"], _event())
    assert (child._trust, child._trust_reads) == before


def test_the_switch_defaults_on_and_fails_closed(monkeypatch) -> None:
    # The real reader (imported before the autouse fixture replaced the module name).
    assert KiroCrewConfig().agent.conductor_own_child_verbs is True

    def _load(value: bool, degraded: frozenset = frozenset()):
        cfg = SimpleNamespace(
            agent=SimpleNamespace(conductor_own_child_verbs=value), degraded_sections=degraded
        )
        return classmethod(lambda cls: cfg)

    monkeypatch.setattr(KiroCrewConfig, "load", _load(True))
    assert _conductor_own_child_verbs_enabled() is True
    monkeypatch.setattr(KiroCrewConfig, "load", _load(False))
    assert _conductor_own_child_verbs_enabled() is False
    for lost in ("agent", DEGRADED_WHOLE_CONFIG):
        monkeypatch.setattr(KiroCrewConfig, "load", _load(True, frozenset({lost})))
        assert _conductor_own_child_verbs_enabled() is False

    def _boom(cls):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert _conductor_own_child_verbs_enabled() is False


# --- Through the decider: the rung's place among the others ------------------------


def _decide(
    *, target: str = "w-cond", backend: str = "", deny: bool = False, native_child: bool = False
):
    """Run ``_decide_permission_request`` for ``cond``'s ``session_send`` to *target*.

    Returns (approved by the auto-approve rung, the audit rows' outcome and metadata).
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult

    state = _state()
    state.context_builder.hooks.on_tool_call = lambda *a, **k: ToolHookResult(action=TOOL_ALLOW)
    state.is_yolo_active = lambda: False
    state.broadcast_ws = lambda *a, **k: None
    slot = MagicMock(key="cond", agent="kirocrew-conductor", _app="", _trust=False)
    slot._trust_reads = False
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock(return_value=True)
    turn = MagicMock(client=client, slot=slot, state=state, session_key="dashboard:cond")
    turn._native_tracker, turn._native_child_tc = {}, set()
    turn._refusal_notices, turn._refusal_reasons = [], []
    turn._wt_pending_coding, turn.assistant_text = set(), ""
    # A person's turn, not a Dashboard card's (a bare MagicMock attribute is truthy).
    turn._card_origin = False
    event = MagicMock(**vars(_event(target=target)))
    event.tool_call_id, event.title, event.request_id, event.tool_kind = "", "send", "r1", "other"
    event.child_unconditional_grant_eligible, event.child_low_fidelity = True, False
    event.native_child_call = native_child
    rows: list[dict] = []
    sel = MagicMock()
    sel.return_value.log_tool_invocation = lambda **k: rows.append(k)
    # PreToolUse hooks run (none configured) and pass.
    names: dict = {
        "sel": sel,
        "_broadcast_auto_tool": lambda *a: "send",
        "_fire": AsyncMock(return_value=[]),
    }
    if backend:
        names["capabilities_of"] = lambda c: SimpleNamespace(backend=backend)
    if deny:
        state.context_builder.hooks.on_tool_call = lambda *a, **k: ToolHookResult(
            action=TOOL_DENY, reason="no"
        )
        names["client_accepts_deny_waivers"] = lambda c: False
    decide = tool_approval._decide_permission_request
    with patch.dict(decide.__globals__, names):
        asyncio.run(decide(turn, event))
    return client.approve_tool.await_count == 1, [
        (r.get("outcome"), (r.get("metadata") or {}).get("reason")) for r in rows
    ]


def test_the_decider_approves_its_own_child_and_audits_the_reason() -> None:
    assert _decide() == (True, [("auto_approved", "conductor_own_child")])


def test_the_decider_still_asks_for_a_session_it_did_not_create() -> None:
    assert _decide(target="tab") == (False, [])


def test_a_native_sub_agent_call_still_asks() -> None:
    # A native KAS child's request names the parent's slot; it is not the parent's own.
    assert _decide(native_child=True) == (False, [])


def test_the_claude_backend_still_asks() -> None:
    # Its permission requests do not say which session sent them.
    assert _decide(backend="claude") == (False, [])


def test_a_deny_rule_runs_first() -> None:
    assert _decide(deny=True) == (False, [("denied", None)])
