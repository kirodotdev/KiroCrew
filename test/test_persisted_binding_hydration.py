"""The persisted-binding gate as HYDRATION sees it, at both adoption sites.

``test_persisted_binding_gate`` pins the predicate. This file pins what the predicate
exists for: that a forged ``linked_session_key`` on the agent-writable metadata line
does not survive a restart into ``slot.linked_session_key``, which decides where the
slot's turns are ROUTED.

Two independent builders adopt it -- ``_rehydrate_slot_from_history`` (open-tab
restore) and ``_apply_recent_session`` (recent/pinned restore) -- and a gate wired
into only one leaves the other adopting unchecked, so every case is asserted through
BOTH.

Each refusal test carries a positive control: the same hydration with a LEGITIMATE
binding must still adopt, or a gate stuck at "refuse everything" would pass every
refusal assertion while unbinding every channel tab.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

#: A key naming a conversation that is NOT the transcript being hydrated -- the
#: forgery the gate exists to refuse.
FOREIGN_KEY = "slack:C999:1700000000.999999"


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _persisted_slot(state, name: str = "s1"):
    """A saved slot with one row, i.e. a transcript a restore path will pick up."""
    slot = state.get_or_create_slot(name)
    slot.append("user", "a turn that belongs to this transcript")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    return slot


def _plant_binding(state, candidate: str, name: str = "s1") -> None:
    """Write *candidate* onto the persisted metadata line, as an agent could."""
    state.conversation_log.update_metadata(f"dashboard:{name}", {"linked_session_key": candidate})


def _restore_via_open_tab(state, name: str = "s1"):
    del state._slots[name]
    return _rehydrate_slot_from_history(state, name)


def _restore_via_recent(state, name: str = "s1"):
    del state._slots[name]
    assert restore_recent_sessions(state, window_minutes=0) >= 1
    return state._slots[name]


#: Both adoption sites, driven through their real entry points. Parametrized rather
#: than duplicated so a site added later fails one list rather than being missed.
RESTORE_PATHS = [
    pytest.param(_restore_via_open_tab, id="open-tab restore"),
    pytest.param(_restore_via_recent, id="recent/pinned restore"),
]


@pytest.mark.parametrize("restore", RESTORE_PATHS)
def test_hydration_refuses_a_binding_naming_a_foreign_transcript(restore, tmp_path) -> None:
    """A forged binding leaves the slot UNBOUND rather than rerouting its turns.

    This is the defect the PR is named for. Before the gate was consulted here, the
    assignment was unconditional: a metadata line naming any other session rebound the
    slot on the next restart, silently, and the user's turns went to that conversation
    while the tab kept showing this transcript.

    Unbound is the intended outcome, not a second bug -- the slot answers from its own
    dashboard session, which is visible and re-linkable, where a wrong binding is
    neither.
    """
    state = _make_state(tmp_path)
    _persisted_slot(state)
    _plant_binding(state, FOREIGN_KEY)

    restored = restore(state)

    assert restored is not None
    assert restored.linked_session_key == "", (
        f"hydration adopted {restored.linked_session_key!r} from an agent-writable metadata "
        f"line naming a foreign transcript; this slot's turns would now be routed there"
    )


@pytest.mark.parametrize("restore", RESTORE_PATHS)
def test_hydration_still_adopts_a_binding_naming_its_own_transcript(restore, tmp_path) -> None:
    """POSITIVE CONTROL for the refusal above: a genuine binding must survive.

    Without this, a gate that refused unconditionally would pass every refusal
    assertion in this file while unbinding every channel tab in production -- the
    failure mode the gate's own docstring calls out as the real cost of the rule.
    """
    state = _make_state(tmp_path)
    _persisted_slot(state)
    own_key = "dashboard:s1"
    _plant_binding(state, own_key)

    restored = restore(state)

    assert restored is not None
    assert restored.linked_session_key == own_key, (
        "hydration refused a binding that names its own transcript, so a legitimately "
        "linked slot comes back unbound and its channel thread stops seeing replies"
    )


@pytest.mark.parametrize("restore", RESTORE_PATHS)
def test_hydration_leaves_an_unbound_slot_alone(restore, tmp_path) -> None:
    """Metadata carrying NO binding is not a decision, and must not become one.

    The gate's "no candidate" answer and its "refused" answer are different values for
    a reason: conflating them would make every ordinary dashboard slot take the refusal
    branch and log a warning about a binding it never had.
    """
    state = _make_state(tmp_path)
    _persisted_slot(state)

    restored = restore(state)

    assert restored is not None
    assert restored.linked_session_key == ""


def test_a_refused_adoption_is_recorded_in_the_security_event_log(tmp_path, monkeypatch) -> None:
    """The refusal is audited, not merely logged.

    A logger line is rotated, unsigned and outside the verifiable chain, so it cannot
    evidence a permission decision about agent-writable state. The audit is what makes
    a forged binding attempt visible after the fact.
    """
    recorded: list[dict] = []

    def _capture(**kwargs):
        recorded.append(kwargs)

    sel = MagicMock(log_governance_decision=MagicMock(side_effect=_capture))
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.sel", lambda: sel)

    state = _make_state(tmp_path)
    _persisted_slot(state)
    _plant_binding(state, FOREIGN_KEY)

    _restore_via_open_tab(state)

    denials = [r for r in recorded if r.get("outcome") == "denied"]
    assert denials, f"no denial row reached the SEL; rows were {recorded}"
    assert denials[0]["item"] == FOREIGN_KEY
    assert denials[0]["tool_name"] == "chat:adopt_persisted_binding"


def test_hydration_refuses_when_the_adoption_cannot_be_audited(tmp_path, monkeypatch) -> None:
    """An unwritable SEL REFUSES the adoption rather than adopting unrecorded.

    Audit-or-deny: the record has to land before the decision is acted on, so a failing
    SEL is a refusal and not a warning-and-proceed. Asserted through hydration because
    the predicate alone cannot show which way the builder falls when the audit fails.
    """
    sel = MagicMock(log_governance_decision=MagicMock(side_effect=OSError("read-only")))
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.sel", lambda: sel)

    state = _make_state(tmp_path)
    _persisted_slot(state)
    # A binding that WOULD be adopted if the audit had landed, so the refusal below is
    # attributable to the failed write and not to the candidate.
    _plant_binding(state, "dashboard:s1")

    restored = _restore_via_open_tab(state)

    assert restored is not None
    assert restored.linked_session_key == ""


@pytest.mark.asyncio
async def test_the_async_restore_decides_the_binding_off_the_event_loop(
    tmp_path, monkeypatch
) -> None:
    """The async path pre-audits in a worker thread instead of deciding in the build.

    The SEL write for a critical audit is inline, and the synchronous build is
    deliberately await-free -- an await between its deletion probe and the build itself
    reopens the window that probe closes. So the verdict is resolved BEFORE the build
    and handed in. Pinned by asserting the builder was given a decision rather than
    reaching for one, which is the part a behavioural assertion cannot see.
    """
    from kiro_crew.dashboard import chat_persistence

    state = _make_state(tmp_path)
    _persisted_slot(state)
    _plant_binding(state, FOREIGN_KEY)
    del state._slots["s1"]

    inline_calls: list[tuple] = []
    real_decide = chat_persistence.decide_persisted_binding
    monkeypatch.setattr(
        chat_persistence,
        "decide_persisted_binding",
        lambda *a, **k: (inline_calls.append(a), real_decide(*a, **k))[1],
    )

    restored = await chat_persistence.rehydrate_slot_from_history_async(state, "s1")

    assert restored is not None
    assert restored.linked_session_key == ""
    assert not inline_calls, (
        "the async path fell through to the inline decision, so a critical SEL write "
        "ran on the event loop during slot construction"
    )
