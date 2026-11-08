"""A Crew Mode conductor's own READS of ITS crew members need no per-call approval.

Only the verified ``kirocrew-dashboard`` session_status / session_read_message
identity with a verified ``target`` that is the exact key of a live session whose
GATEWAY-VOUCHED creator is the calling conductor qualifies: the settled session
tree's edge, else a birth stamp this process minted, never the saved ``created_by``
a restore reads from editable transcript metadata. A ``session_send`` is never on
the list (the conductor skill pins that a send is not auto-approved). Everything
else asks.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import crew_mode
from kiro_crew.dashboard.chat_turn.tool_approval import _crew_root_own_worker_call


@pytest.fixture(autouse=True)
def _unsettled_lineage(_floor_monkeypatch):
    # The crew log off: no session tree, so the birth stamp with its mint witness decides.
    _floor_monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    # The routing is off on this base (the Crew panel part turns it on); the rule
    # is tested as it will run then.
    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", True)


def _slot(
    key: str,
    *,
    agent: str = "default",
    created_by: str = "",
    minted: bool = True,
    saved_created_by: str = "",
    trust: bool = False,
    trust_reads: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent,
        agent_kind="template",
        mode="",
        memory_mode="persistent",
        _created_by=created_by,
        _saved_created_by=saved_created_by,
        _lineage_minted=minted,
        _trust=trust,
        _trust_reads=trust_reads,
        _app="",
        instance_id="",
        crew_mode_agent="",
    )


def _state() -> SimpleNamespace:
    root = _slot("root-1", agent="kirocrew-conductor")
    other_root = _slot("root-2", agent="kirocrew-conductor")
    return SimpleNamespace(
        _slots={
            "root-1": root,
            "w-1": _slot("w-1", agent="kirocrew-worker", created_by="root-1"),
            "root-2": other_root,
            "w-2": _slot("w-2", agent="kirocrew-worker", created_by="root-2"),
            "plain": _slot("plain"),
            # Reopened after a restart: the creator is known only from the saved
            # metadata line, which an agent's file tools can edit.
            "restored": _slot(
                "restored", agent="kirocrew-worker", minted=False, saved_created_by="root-1"
            ),
            # A restored birth stamp with no mint witness is the same unvouched claim.
            "unminted": _slot(
                "unminted", agent="kirocrew-worker", created_by="root-1", minted=False
            ),
            "trusted": _slot("trusted", agent="kirocrew-worker", created_by="root-1", trust=True),
            "reads": _slot("reads", agent="kirocrew-worker", created_by="root-1", trust_reads=True),
        }
    )


def _event(tool: str = "session_status", target: object = "w-1", **over: object) -> SimpleNamespace:
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


@pytest.mark.parametrize("tool", ["session_status", "session_read_message"])
def test_conductor_call_to_its_own_worker_is_granted(tool: str) -> None:
    state = _state()
    assert _crew_root_own_worker_call(state, state._slots["root-1"], _event(tool)) is True


def test_a_deferred_tool_call_reads_the_target_from_its_arguments() -> None:
    state = _state()
    wrapped = {
        "tool_id": "kirocrew-dashboard::session_status",
        "arguments": {"target": "w-1"},
    }
    assert (
        _crew_root_own_worker_call(state, state._slots["root-1"], _event(raw_tool_params=wrapped))
        is True
    )
    other = {"tool_id": "kirocrew-dashboard::session_stop", "arguments": {"target": "w-1"}}
    assert (
        _crew_root_own_worker_call(state, state._slots["root-1"], _event(raw_tool_params=other))
        is False
    )


@pytest.mark.parametrize(
    "caller, event",
    [
        ("root-1", _event(target="w-2")),  # another conductor's worker
        ("root-1", _event(target="plain")),  # a plain session
        ("root-1", _event(target="root-1")),  # itself
        ("root-1", _event(target="Worker title")),  # by title, not key
        ("root-1", _event(tool="session_stop")),  # a verb not on the list
        ("root-1", _event(tool="session_send")),  # a send is never auto-approved
        ("root-1", _event(tool="session_send", target="trusted")),
        ("root-1", _event(mcp_server_name="kirocrew-core")),
        ("root-1", _event(mcp_identity_trusted=False)),
        ("root-1", _event(raw_params_trusted=False)),
        ("root-1", _event(sub_session_id="sub-1")),  # a subagent's call
        ("plain", _event(target="w-1")),  # caller is no conductor
        ("root-1", _event(target="restored")),  # creator known from saved metadata only
        ("root-1", _event(tool="session_status", target="restored")),
        ("root-1", _event(target="unminted")),  # a birth stamp this process never minted
    ],
)
def test_anything_else_still_asks(caller: str, event: SimpleNamespace) -> None:
    state = _state()
    assert _crew_root_own_worker_call(state, state._slots[caller], event) is False


@pytest.mark.parametrize("tool", ["session_status", "session_read_message"])
def test_a_read_of_a_trusted_crew_member_is_still_granted(tool: str) -> None:
    # A read runs nothing in the target; only a send carries text into its turn.
    state = _state()
    assert (
        _crew_root_own_worker_call(state, state._slots["root-1"], _event(tool, "trusted")) is True
    )


def test_with_a_settled_tree_the_edge_decides_not_the_stamp(monkeypatch) -> None:
    """The crew log's session tree is the record: an adoption moves a crew member to its
    new root, a release clears its edge, and a slot the tree never saw is no one's."""
    state = _state()
    monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset({"w-1", "w-2"}))
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"w-2": "root-1"})
    root = state._slots["root-1"]
    # w-2 was adopted by root-1: the edge grants, whatever the birth stamp says.
    assert _crew_root_own_worker_call(state, root, _event(target="w-2")) is True
    # w-1 is in the tree with no parent (released): the birth stamp does not revive it.
    assert _crew_root_own_worker_call(state, root, _event(target="w-1")) is False
    # A slot the tree never saw falls back to its minted birth stamp only.
    assert _crew_root_own_worker_call(state, root, _event(target="trusted")) is True
    assert _crew_root_own_worker_call(state, root, _event(target="restored")) is False


def test_the_rule_is_off_until_the_crew_panel_part_turns_it_on(_floor_monkeypatch) -> None:
    """On this base ``crew_mode.APPROVAL_ROUTING_LIVE`` is False, so even the
    conductor's own read of its crew member asks, as every conductor does today."""
    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", False)
    state = _state()
    assert _crew_root_own_worker_call(state, state._slots["root-1"], _event()) is False
