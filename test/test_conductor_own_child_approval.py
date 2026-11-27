"""A conductor's ``session_send`` / ``session_close`` into a session it created needs no
per-call approval (rfc-conductor-own-child-verbs.md, section 5.2).

Only ``kirocrew-conductor``, ``kirocrew-team-lead`` and a Bot's conducting worker
(``members.conducting_worker_session``; rfc-bot-workers-conduct.md, section 5.3)
qualify, only for those two verbs, only with a verified ``kirocrew-dashboard``
identity and arguments, and only when the ``target`` is the exact key of a live
session whose GATEWAY-VOUCHED creator is the caller.
``agent.conductor_own_child_verbs`` turns it off. Everything else asks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import crew_mode, members
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard import chat_runner  # noqa: F401  (composes tool_approval's names)
from kiro_crew.dashboard.chat_turn import tool_approval
from kiro_crew.dashboard.chat_turn.tool_approval import (
    _conductor_own_child_call,
    _conductor_own_child_verbs_enabled,
)
from kiro_crew.hook_runtime.hook_types import HooksConfig

_RULE_NS = _conductor_own_child_call.__globals__


#: The live provider of each running session, by session key: what its handle took
#: at its start (``member_dispatch_mounted``). Filled by :func:`_start`.
_LIVE: dict[str, SimpleNamespace] = {}


@pytest.fixture(autouse=True)
def _defaults(_floor_monkeypatch):
    _LIVE.clear()
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


def _start(key: str) -> None:
    """(Re)start *key*'s session: its live provider reports whether the composer
    mounted session control at that start, decided by the predicate every composer
    reads (``members.mounts_session_control``) at the moment of the start."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    mounted = members.mounts_session_control(key)
    _LIVE[effective_session_key(_slot(key))] = SimpleNamespace(member_dispatch_mounted=mounted)


def _state(**hooks: object) -> SimpleNamespace:
    return SimpleNamespace(
        context_builder=_builder(**hooks),
        sessions=SimpleNamespace(get_provider=_LIVE.get),
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
            # A Bot's thread, two workers it opened on the Bot's worker template and a
            # child each worker opened. Which worker conducts is decided by the
            # registry (``conducting_bot_worker`` below), never by the template.
            "member-pixel": _slot("member-pixel", agent="kirocrew-mate-conductor"),
            "w-bot": _slot("w-bot", agent="kirocrew-mate-worker", created_by="member-pixel"),
            "w-plainbot": _slot(
                "w-plainbot", agent="kirocrew-mate-worker", created_by="member-pixel"
            ),
            "gc-bot": _slot("gc-bot", agent="kirocrew-mate-worker", created_by="w-bot"),
            "gc-plainbot": _slot(
                "gc-plainbot", agent="kirocrew-mate-worker", created_by="w-plainbot"
            ),
        },
    )


@pytest.fixture
def conducting_bot_worker(monkeypatch):
    """``w-bot`` conducts through the REAL registry: thread-born, known as pixel's
    worker, pixel's record has the switch on, and its session started with the
    session-control mount. ``w-plainbot`` is the control on the same template:
    known as pixel's worker but never marked."""
    members.note_worker_lineage("w-bot", "member-pixel")
    members.note_crewmate_worker_session("w-bot", "member-id-pixel")
    members.note_crewmate_worker_session("w-plainbot", "member-id-pixel")
    cfg = SimpleNamespace(
        agents={
            "pixel": KiroCrewAgentConfig(
                kiro_agent="pixel", member_id="member-id-pixel", worker_conducts=True
            )
        },
        degraded_sections=frozenset(),
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    _start("w-bot")
    _start("w-plainbot")
    return cfg


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


@pytest.mark.parametrize("tool", ["session_send", "session_close"])
def test_conductor_modes_default_agent_sends_and_closes_its_own_child_unprompted(tool) -> None:
    assert crew_mode.DEFAULT_CREW_MODE_AGENT == "kirocrew-conductor"
    state = _state()
    root = _slot("root", agent=crew_mode.DEFAULT_CREW_MODE_AGENT)
    state._slots["root"] = root
    state._slots["w-root"] = _slot("w-root", agent="kirocrew-worker", created_by="root")
    assert _conductor_own_child_call(state, root, _event(tool, "w-root")) is True
    # Another root's child still prompts.
    assert _conductor_own_child_call(state, root, _event(tool, "w-cond")) is False


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


# --- A Bot's conducting worker (rfc-bot-workers-conduct.md, section 5.3) -------------


@pytest.mark.parametrize("tool", ["session_send", "session_close"])
def test_a_conducting_worker_sends_and_closes_its_own_child_unprompted(
    conducting_bot_worker, tool
) -> None:
    state = _state()
    assert _conductor_own_child_call(state, state._slots["w-bot"], _event(tool, "gc-bot")) is True


@pytest.mark.parametrize(
    "event",
    [
        _event(target="gc-plainbot"),  # a sibling worker's child
        _event(target="w-cond"),  # a conductor's child
        _event(target="member-pixel"),  # its own Bot's thread
        _event(target="tab"),  # a person's tab
        _event(tool="session_stop", target="gc-bot"),  # a gated verb, as for every conductor
        _event(tool="session_broadcast", target="gc-bot"),
        _event(tool="work_dispatch", target="gc-bot"),
        _event(target="gc-bot", sub_session_id="sub-1"),  # its subagent's call
        _event(target="gc-bot", mcp_identity_trusted=False),
    ],
)
def test_a_conducting_worker_still_asks_for_anything_else(
    conducting_bot_worker, event: SimpleNamespace
) -> None:
    state = _state()
    assert _conductor_own_child_call(state, state._slots["w-bot"], event) is False


@pytest.mark.parametrize("tool", ["session_send", "session_close"])
def test_an_unmarked_worker_on_the_same_template_never_gets_the_exemption(
    conducting_bot_worker, tool
) -> None:
    """Admitted by the conducting kit, never by ``kirocrew-mate-worker``: a worker
    the thread opened that is not thread-born (here: never marked) prompts for its
    own child, switch on or not, and even with a mount on its running handle."""
    state = _state()
    plain = state._slots["w-plainbot"]
    assert _conductor_own_child_call(state, plain, _event(tool, "gc-plainbot")) is False
    from kiro_crew.dashboard.chat_utils import effective_session_key

    _LIVE[effective_session_key(plain)] = SimpleNamespace(member_dispatch_mounted=True)
    assert _conductor_own_child_call(state, plain, _event(tool, "gc-plainbot")) is False


@pytest.mark.parametrize("tool", ["session_send", "session_close"])
def test_the_switch_off_reaches_the_worker_at_its_next_start(conducting_bot_worker, tool) -> None:
    """Keyed on the kit the worker's session started with, never the live switch:
    turned off mid-task, its own-child send or close still runs without a prompt;
    from its next start (the reload the owner's save brings) it prompts."""
    conducting_bot_worker.agents["pixel"].worker_conducts = False
    state = _state()
    worker = state._slots["w-bot"]
    assert _conductor_own_child_call(state, worker, _event(tool, "gc-bot")) is True
    _start("w-bot")
    assert _conductor_own_child_call(state, worker, _event(tool, "gc-bot")) is False


def test_the_switch_on_reaches_a_running_plain_worker_at_its_next_start(
    conducting_bot_worker,
) -> None:
    """A thread-born worker whose session started with the switch off keeps prompting
    until its next start, however the switch moves meanwhile."""
    conducting_bot_worker.agents["pixel"].worker_conducts = False
    _start("w-bot")
    conducting_bot_worker.agents["pixel"].worker_conducts = True
    state = _state()
    worker = state._slots["w-bot"]
    assert _conductor_own_child_call(state, worker, _event("session_send", "gc-bot")) is False
    _start("w-bot")
    assert _conductor_own_child_call(state, worker, _event("session_send", "gc-bot")) is True


def test_the_operator_switch_off_covers_the_conducting_worker_too(
    conducting_bot_worker, _floor_monkeypatch
) -> None:
    _floor_monkeypatch.setitem(_RULE_NS, "_conductor_own_child_verbs_enabled", lambda: False)
    state = _state()
    for tool in ("session_send", "session_close"):
        assert not _conductor_own_child_call(state, state._slots["w-bot"], _event(tool, "gc-bot"))


def test_a_grandchild_is_no_conductor(conducting_bot_worker) -> None:
    """One conducting level, by lineage: the conducting worker's own child was
    created by a ``chat-`` key, so it holds no mark and prompts for everything."""
    members.note_crewmate_worker_session("gc-bot", "member-id-pixel")
    members.note_worker_lineage("gc-bot", "w-bot")
    state = _state()
    state._slots["ggc-bot"] = _slot("ggc-bot", agent="kirocrew-mate-worker", created_by="gc-bot")
    assert (
        _conductor_own_child_call(state, state._slots["gc-bot"], _event(target="ggc-bot")) is False
    )


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
    *,
    target: str = "w-cond",
    backend: str = "",
    deny: bool = False,
    native_child: bool = False,
    caller: str = "cond",
    agent: str = "kirocrew-conductor",
):
    """Run ``_decide_permission_request`` for *caller*'s ``session_send`` to *target*.

    Returns (approved by the auto-approve rung, the audit rows' outcome and metadata).
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult

    state = _state()
    state.context_builder.hooks.on_tool_call = lambda *a, **k: ToolHookResult(action=TOOL_ALLOW)
    state.is_yolo_active = lambda: False
    state.broadcast_ws = lambda *a, **k: None
    slot = MagicMock(key=caller, agent=agent, _app="", _trust=False)
    slot._trust_reads = False
    # A dashboard slot of its own: its session is ``dashboard:<caller>``.
    slot.linked_session_key = ""
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock(return_value=True)
    turn = MagicMock(client=client, slot=slot, state=state, session_key=f"dashboard:{caller}")
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
        names["capabilities_of"] = lambda c: capabilities_for(backend)
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


def test_the_decider_approves_a_conducting_workers_own_child(conducting_bot_worker) -> None:
    worker = dict(caller="w-bot", agent="kirocrew-mate-worker", target="gc-bot")
    assert _decide(**worker) == (True, [("auto_approved", "conductor_own_child")])
    # The same template, unmarked: the rung does not fire.
    plain = dict(caller="w-plainbot", agent="kirocrew-mate-worker", target="gc-plainbot")
    assert _decide(**plain) == (False, [])


def test_the_claude_backend_still_asks_a_conducting_worker(conducting_bot_worker) -> None:
    """A Bot pinned to claude gets the mount and the grants, and every seed and
    close prompts (rfc-bot-workers-conduct.md, section 10)."""
    worker = dict(caller="w-bot", agent="kirocrew-mate-worker", target="gc-bot")
    assert _decide(backend="claude", **worker) == (False, [])


def test_a_deny_rule_runs_first() -> None:
    assert _decide(deny=True) == (False, [("denied", None)])
