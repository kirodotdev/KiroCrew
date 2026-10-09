"""Three change-card behaviours.

* O1: a capabilities draft that launches a server is ``code_exec`` yet still
  needs the separate widen acknowledgement, pinned by ``rebuild_record``.
* O2: ``dashboard.terminal.shell`` is ``code_exec``.
* O3: housekeeping cancels a pending card only on positive closure evidence
  after the open-tab restore has run, never because a slot is absent.

Everything runs in-process.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard import channel_slots, chat_utils
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes

SLOT = "chat-1"
SK = f"dashboard:{SLOT}"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


# A capabilities draft that BOTH sets an MCP server (a launch line = code_exec)
# AND auto-approves one of its tools (approval_expanded = widen).
_CAP_LAUNCH_AND_APPROVE = {
    "member": "mate",
    "draft": {
        "operations": [
            {
                "section": "mcpServers",
                "id": "helper",
                "action": "set",
                "value": {"command": "sh", "args": ["-c", "echo hi"]},
            },
            {"section": "autoApprove", "id": "@helper/run", "action": "set", "value": True},
        ]
    },
}
_CAP_BEFORE = {
    "revision": "r1",
    "rows": [
        {"section": "mcpServers", "id": "helper", "state": "inherited", "value": None},
        {"section": "autoApprove", "id": "@helper/run", "state": "inherited", "value": None},
    ],
}
_CAP_IMPACT = [
    {"section": "mcpServers", "id": "helper", "change": "added"},
    {"section": "autoApprove", "id": "@helper/run", "change": "added", "approval_expanded": True},
]


# ── O1: code_exec wins the risk label; widen is carried alongside ──


def test_a_capabilities_draft_that_launches_and_approves_is_code_exec_but_still_widens():
    p = catalog.validate_params("crewmate.capabilities", _CAP_LAUNCH_AND_APPROVE)
    preview = catalog.build_preview(
        "crewmate.capabilities", p, _CAP_BEFORE, {"impact": _CAP_IMPACT}
    )
    # The badge shows the single worst label.
    assert preview["risk"] == catalog.RISK_CODE_EXEC
    # But the auto-approval acknowledgement is still required: the flag is
    # carried separately so it is not folded into (and lost behind) code_exec.
    assert preview["widen"] is True


def test_a_plain_approval_expansion_is_widen_and_sets_the_flag():
    draft = {
        "member": "mate",
        "draft": {
            "operations": [
                {"section": "autoApprove", "id": "@helper/run", "action": "set", "value": True}
            ]
        },
    }
    before = {
        "revision": "r1",
        "rows": [{"section": "autoApprove", "id": "@helper/run", "state": "inherited"}],
    }
    impact = [
        {
            "section": "autoApprove",
            "id": "@helper/run",
            "change": "added",
            "approval_expanded": True,
        }
    ]
    p = catalog.validate_params("crewmate.capabilities", draft)
    preview = catalog.build_preview("crewmate.capabilities", p, before, {"impact": impact})
    assert preview["risk"] == catalog.RISK_WIDEN
    assert preview["widen"] is True


def test_a_launch_without_approval_expansion_is_code_exec_and_does_not_widen():
    draft = {
        "member": "mate",
        "draft": {
            "operations": [
                {
                    "section": "mcpServers",
                    "id": "helper",
                    "action": "set",
                    "value": {"command": "sh", "args": ["-c", "echo hi"]},
                }
            ]
        },
    }
    before = {
        "revision": "r1",
        "rows": [{"section": "mcpServers", "id": "helper", "state": "inherited"}],
    }
    impact = [{"section": "mcpServers", "id": "helper", "change": "added"}]
    p = catalog.validate_params("crewmate.capabilities", draft)
    preview = catalog.build_preview("crewmate.capabilities", p, before, {"impact": impact})
    assert preview["risk"] == catalog.RISK_CODE_EXEC
    # No auto-approval was granted, so no widen acknowledgement is required.
    assert preview["widen"] is False


def test_every_preview_carries_a_widen_default_of_false():
    p = catalog.validate_params("setting.change", {"path": "agent.model", "value": "x"})
    preview = catalog.build_preview("setting.change", p, {"value": "y"}, {})
    assert preview["widen"] is False


def test_widen_flows_through_derived_fields_into_the_public_card():
    p = catalog.validate_params("crewmate.capabilities", _CAP_LAUNCH_AND_APPROVE)
    preview = catalog.build_preview(
        "crewmate.capabilities", p, _CAP_BEFORE, {"impact": _CAP_IMPACT}
    )
    fields = cards.derived_fields("crewmate.capabilities", preview, {})
    assert fields["risk"] == catalog.RISK_CODE_EXEC
    assert fields["widen"] is True
    assert "widen" in cards._DERIVED_KEYS

    store = CardStore(None, clock=Clock())
    rec = store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="crewmate.capabilities",
        params=p,
        reason="",
        preview=preview,
        before=_CAP_BEFORE,
        context={"impact": _CAP_IMPACT},
    )
    public = store.public(rec)
    assert public["risk"] == catalog.RISK_CODE_EXEC
    assert public["widen"] is True


def test_rebuild_record_drops_a_record_whose_stored_widen_was_flipped():
    """``widen`` is a derived field, so a persisted record whose ``widen`` the
    agent edited (to drop the acknowledgement) fails the rebuild and is refused."""
    p = catalog.validate_params("crewmate.capabilities", _CAP_LAUNCH_AND_APPROVE)
    preview = catalog.build_preview(
        "crewmate.capabilities", p, _CAP_BEFORE, {"impact": _CAP_IMPACT}
    )
    store = CardStore(None, clock=Clock())
    rec = store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="crewmate.capabilities",
        params=p,
        reason="",
        preview=preview,
        before=_CAP_BEFORE,
        context={"impact": _CAP_IMPACT},
    )
    assert cards.rebuild_record(dict(rec)) is not None
    tampered = dict(rec)
    tampered["widen"] = False
    assert cards.rebuild_record(tampered) is None


def test_rebuild_record_admits_an_old_authenticated_card_without_a_widen_field():
    """A stored record with no ``widen`` key is admitted and gets it derived."""
    p = catalog.validate_params("crewmate.capabilities", _CAP_LAUNCH_AND_APPROVE)
    preview = catalog.build_preview(
        "crewmate.capabilities", p, _CAP_BEFORE, {"impact": _CAP_IMPACT}
    )
    store = CardStore(None, clock=Clock())
    rec = store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="crewmate.capabilities",
        params=p,
        reason="",
        preview=preview,
        before=_CAP_BEFORE,
        context={"impact": _CAP_IMPACT},
    )
    legacy = dict(rec)
    legacy.pop("widen", None)  # a card persisted before widen was derived
    assert "widen" not in legacy
    rebuilt = cards.rebuild_record(legacy)
    # The absent legacy flag is permitted: the record survives...
    assert rebuilt is not None
    # ...and widen is derived back onto it rather than left missing.
    assert rebuilt["widen"] is True


# ── O2: dashboard.terminal.shell is code_exec ──


def test_setting_the_dashboard_terminal_shell_is_code_exec():
    p = catalog.validate_params(
        "setting.change", {"path": "dashboard.terminal.shell", "value": "/bin/zsh"}
    )
    preview = catalog.build_preview(
        "setting.change",
        p,
        {"store": catalog.STORE_KIROCREW, "key": "dashboard.terminal.shell", "value": "/bin/bash"},
        {},
    )
    assert preview["risk"] == catalog.RISK_CODE_EXEC


def test_the_terminal_shell_is_code_exec_in_either_direction():
    # A free-form command string has no widen/tighten order; it runs code either way.
    for before, after in (("/bin/bash", "/bin/zsh"), ("/bin/zsh", "/bin/sh")):
        assert catalog._setting_risk("dashboard.terminal.shell", before, after) == (
            catalog.RISK_CODE_EXEC
        )


def test_an_ordinary_dashboard_setting_is_not_code_exec():
    # The code_exec rule is scoped to the shell key, not every dashboard setting.
    assert catalog._setting_risk("dashboard.folder_sort", "name", "recent") == catalog.RISK_NORMAL


# ── O3: _settle retires only genuinely-closed slots ──


class _MetaLog:
    """Stands in for ``ConversationLog.get_metadata_status``."""

    def __init__(self) -> None:
        # Keyed by transcript key (slot_transcript_key(slot_name)).
        self.closed_keys: set[str] = set()
        self.unreadable_keys: set[str] = set()

    def get_metadata_status(self, history_key: str):
        if history_key in self.unreadable_keys:
            return None, False
        if history_key in self.closed_keys:
            return {"closed": True}, True
        return {}, True


class _SettleState:
    """The slice of DashboardState ``_settle`` reads."""

    owner_id = ""

    def __init__(self, store: CardStore) -> None:
        self._change_card_store = store
        # False by default: the pre-restore window, where absence proves nothing.
        self.open_slots_restored = False
        self.restoring_open_slots = False
        self.unrestored_slot_keys: set[str] = set()
        self.conversation_log = _MetaLog()
        self.frames: list[tuple[str, dict[str, Any]]] = []

    # ``_slot_is_open`` structurally never calls ``get_slot``: retirement reads
    # the tombstone and the passed-in persisted-closed set, never a bare lookup.
    # ``get_slot`` here exists only for ``chat_cards.record_change_status`` on
    # the broadcast of a card that DID retire; it returns None like the real one
    # for a slot already gone. The guard that retirement ignores it is
    # ``_get_slot_banned`` below, used by the dedicated regression test.
    def get_slot(self, name: str):
        return None

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        return 1


class _FakeApp(dict):
    pass


class _FakeRequest:
    def __init__(self, state: _SettleState) -> None:
        self.app = _FakeApp(state=state)


def _pending_card(store: CardStore, slot_key: str = SLOT) -> dict[str, Any]:
    params = catalog.validate_params("setting.change", {"path": "agent.model", "value": "x"})
    preview = catalog.build_preview("setting.change", params, {"value": "y"}, {})
    return store.propose(
        slot_key=slot_key,
        session_key=f"dashboard:{slot_key}",
        kind="setting.change",
        params=params,
        reason="",
        preview=preview,
        before={"value": "y"},
        context={},
    )


def test_settle_does_not_cancel_before_the_restore_has_run():
    """Pre-restore: both flags False. ``slot_exists`` would read this absent
    slot as closed, but retirement must not fire until the restore makes the
    live set authoritative."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    assert state.open_slots_restored is False  # initial false flag
    assert state.restoring_open_slots is False
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_settle_does_not_cancel_while_the_restore_is_in_flight():
    """Restore in flight: ``open_slots_restored`` still False. A tab not yet
    reached is absent; absence proves nothing, so no retire."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.restoring_open_slots = True
    state.open_slots_restored = False
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_settle_does_not_cancel_a_restore_budget_skip_after_restore():
    """After the restore, a slot left in History by the restore budget is absent
    AND not in ``unrestored_slot_keys`` -- yet it was never closed. Absence is
    not closure: the card stays pending."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.open_slots_restored = True
    # A budget skip is NOT recorded as unrestored, and has no tombstone and no
    # persisted close -- the exact gap the old ``slot_exists`` logic retired.
    assert SLOT not in state.unrestored_slot_keys
    assert not channel_slots.slot_closed_since(state, SLOT, 0.0)
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_settle_does_not_cancel_an_unrestored_key_after_restore():
    """``unrestored_slot_keys`` is unresolved, not closed: still open."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.open_slots_restored = True
    state.unrestored_slot_keys = {SLOT}
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_settle_cancels_a_slot_with_an_in_memory_close_tombstone():
    """Explicit closure via the real tombstone channel: ``note_slot_closed``
    records the close instant, ``slot_closed_since`` reads it, and the card
    retires. The slot is otherwise absent -- closure, not absence, drives it."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.open_slots_restored = True
    channel_slots.note_slot_closed(state, SLOT)  # the user closed this tab
    assert channel_slots.slot_closed_since(state, SLOT, 0.0)
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "cancelled"


def test_settle_cancels_a_slot_whose_persisted_meta_is_closed():
    """Explicit closure via the persisted ``closed`` flag, read off the loop
    through ``get_metadata_status`` on the transcript key."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.open_slots_restored = True
    state.conversation_log.closed_keys = {chat_utils.slot_transcript_key(SLOT)}
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "cancelled"


def test_settle_keeps_a_slot_whose_persisted_meta_is_open():
    """Readable metadata without a ``closed`` flag is an OPEN slot, not a closed
    one -- the card survives settlement."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.open_slots_restored = True
    # Default _MetaLog returns ({}, True): readable, not closed.
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_settle_keeps_a_slot_whose_persisted_meta_is_unreadable():
    """An unreadable metadata record is unresolved, not proven closed: open."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)
    state = _SettleState(store)
    state.open_slots_restored = True
    state.conversation_log.unreadable_keys = {chat_utils.slot_transcript_key(SLOT)}
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_settle_never_consults_get_slot_for_retirement():
    """Retirement never treats a missing slot as closed."""
    store = CardStore(None, clock=Clock())
    rec = _pending_card(store)

    class _GetSlotBanned(_SettleState):
        def get_slot(self, name: str):
            raise AssertionError("retirement must not consult get_slot")

    state = _GetSlotBanned(store)
    state.open_slots_restored = True
    asyncio.run(routes._settle(_FakeRequest(state)))
    assert rec["status"] == "pending"


def test_slot_is_open_opens_everything_before_restore():
    """Unit guard on the predicate itself: before the restore every key reads
    open, regardless of tombstones or passed-in closed keys."""
    state = _SettleState(CardStore(None, clock=Clock()))
    state.open_slots_restored = False
    channel_slots.note_slot_closed(state, SLOT)  # even an explicit close
    pred = routes._slot_is_open(state, frozenset({SLOT}))
    assert pred(SLOT) is True


def test_slot_is_open_requires_closure_evidence_after_restore():
    """After the restore: open unless a tombstone or a passed-in closed key
    proves closure. Absence alone keeps it open."""
    state = _SettleState(CardStore(None, clock=Clock()))
    state.open_slots_restored = True
    pred = routes._slot_is_open(state, frozenset())
    assert pred("never-closed") is True
    pred_meta = routes._slot_is_open(state, frozenset({"meta-closed"}))
    assert pred_meta("meta-closed") is False
    channel_slots.note_slot_closed(state, "tombstoned")
    pred_tomb = routes._slot_is_open(state, frozenset())
    assert pred_tomb("tombstoned") is False


def test_housekeep_retires_only_the_slot_that_is_closed():
    store = CardStore(None, clock=Clock())
    open_card = _pending_card(store, "chat-open")
    closed_card = _pending_card(store, "chat-closed")
    changed, _undo = store.housekeep(lambda key: key == "chat-open")
    assert open_card["status"] == "pending"
    assert closed_card["status"] == "cancelled"
    assert [c["id"] for c in changed] == [closed_card["id"]]


def test_reopened_live_slot_keeps_cards_despite_an_old_close(monkeypatch):
    state = _SettleState(CardStore(None, clock=Clock()))
    state.open_slots_restored = True
    channel_slots.note_slot_closed(state, SLOT)
    monkeypatch.setattr(state, "slot_exists", lambda key: key == SLOT, raising=False)
    assert routes._slot_is_open(state, frozenset({SLOT}))(SLOT)
