"""Shared Crew: Slack senders are attributed (never admitted) on every Slack path.

The contract under test (``shared_crew.slack_identity``): a Slack path puts
``meta["slack_sender"] = {"team_id", "enterprise_id", "user_id"}`` on a person's
user row; the two append doors (``ConversationLog.append`` and
``_ChatSlot.append``) turn it into ``meta["author"]``; the first stamped row of a
session records its creator; a missing or malformed key changes nothing.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.shared_crew import attribution as attr
from kiro_crew.shared_crew import slack_identity as si
from kiro_crew.shared_crew.collab import DecisionState
from kiro_crew.shared_crew.collaborators import is_collaborator_actor
from kiro_crew.shared_crew.contracts import (
    MESSAGE_AUTHOR_META_KEY,
    SESSION_ATTRIBUTION_META_KEY,
    SLACK_SENDER_META_KEY,
    ActorContext,
    AttributionAvailability,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    SessionAttribution,
    Transport,
    is_channel_principal,
    principal_id_for,
    principal_or_unknown,
)

ALICE_IDS = {"team_id": "T0TEAM", "enterprise_id": "", "user_id": "U0ALICE"}
BOB_IDS = {"team_id": "T0TEAM", "enterprise_id": "", "user_id": "U0BOB"}


@pytest.fixture(autouse=True)
def _names():
    """The gateway's users.info cache, as ``ChannelHistory.user_name`` serves it."""
    names = {"U0ALICE": "Alice Example", "U0BOB": "Bob Example"}
    si.set_display_name_source(lambda uid: names.get(uid, ""))
    yield names
    si.set_display_name_source(None)


def _row(ids: dict | None) -> dict:
    return {} if ids is None else {SLACK_SENDER_META_KEY: dict(ids)}


def _author(meta: dict) -> Principal:
    return principal_or_unknown(meta.get(MESSAGE_AUTHOR_META_KEY))


# -- (a) the contract: a well-formed slack_sender names the Slack person -----------------


def test_well_formed_sender_names_a_slack_human_principal():
    p = si.sender_principal(ALICE_IDS)
    assert p is not None
    assert p.provider == "slack"
    assert p.issuer == "T0TEAM"
    assert p.subject == "U0ALICE"
    assert p.kind is PrincipalKind.HUMAN
    assert p.provenance is Provenance.SLACK_EVENT
    assert p.display.name == "Alice Example"  # from the users.info cache
    assert p.id == principal_id_for("slack", "T0TEAM", "U0ALICE")


def test_enterprise_id_scopes_the_principal_when_set():
    grid = si.sender_principal({**ALICE_IDS, "enterprise_id": "E0ORG"})
    assert grid is not None and grid.issuer == "E0ORG"
    other_ws = si.sender_principal({**ALICE_IDS, "team_id": "T0OTHER", "enterprise_id": "E0ORG"})
    # One person in an Enterprise Grid org is one principal in every workspace.
    assert other_ws is not None and other_ws.id == grid.id


def test_display_never_falls_back_to_the_user_id_or_an_avatar(_names):
    _names.clear()
    p = si.sender_principal(ALICE_IDS)
    assert p is not None and p.display.name == ""
    assert "U0ALICE" not in json.dumps(p.to_public_dict())
    assert "avatar" not in json.dumps(p.to_public_dict())


@pytest.mark.parametrize("cached", ["U0ALICE", " U0ALICE ", "U0\u200bALICE"])
def test_a_cached_name_that_is_the_user_id_counts_as_no_name(_names, cached):
    # A failed users.info lookup can leave the raw id cached as the "name".
    _names["U0ALICE"] = cached
    p = si.sender_principal(ALICE_IDS)
    assert p is not None and p.display.name == ""
    assert "U0ALICE" not in json.dumps(p.to_public_dict())


def test_a_name_that_only_contains_the_user_id_is_kept(_names):
    _names["U0ALICE"] = "U0ALICE (contractor)"
    p = si.sender_principal(ALICE_IDS)
    assert p is not None and p.display.name == "U0ALICE (contractor)"


def test_display_names_lose_control_and_bidi_characters(_names):
    _names["U0ALICE"] = "Ali\u202ece\x07  Ex\u200bample"
    p = si.sender_principal(ALICE_IDS)
    assert p is not None and p.display.name == "Alice Example"


def test_no_sender_is_written_before_the_install_is_verified(monkeypatch):
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_team_id", "")
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_enterprise_id", "")
    assert si.gateway_sender_meta("U0ALICE", "T0TEAM") is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "U0ALICE",
        {},
        {"team_id": "T0TEAM", "user_id": "B0BOT"},  # a bot id is not a user id
        {"team_id": "", "enterprise_id": "", "user_id": "U0ALICE"},
        {"team_id": "T0TEAM", "enterprise_id": "X1", "user_id": "U0ALICE"},
        {"team_id": "T0TEAM", "enterprise_id": "", "user_id": "u0alice"},
        {"team_id": "T0TEAM", "enterprise_id": "", "user_id": 7},
        {"team_id": "T0TEAM", "enterprise_id": "", "user_id": "U0<script>"},
    ],
)
def test_missing_or_malformed_sender_stamps_nothing(raw):
    meta: dict = {} if raw is None else {SLACK_SENDER_META_KEY: raw}
    assert si.stamp_sender_author(meta) is None
    assert MESSAGE_AUTHOR_META_KEY not in meta


def test_slackbot_and_this_apps_own_user_are_never_human(monkeypatch):
    bot = si.sender_principal({**ALICE_IDS, "user_id": si.SLACKBOT_USER_ID})
    assert bot is not None and bot.kind is PrincipalKind.MACHINE
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_self_user_id", "U0SELF")
    own = si.sender_principal({**ALICE_IDS, "user_id": "U0SELF"})
    assert own is not None and own.kind is PrincipalKind.MACHINE


def test_a_bot_message_writes_no_sender():
    from kiro_crew.slack.handler import _slack_sender_row_meta

    assert _slack_sender_row_meta("U0ALICE", "T0TEAM", True) is None
    assert _slack_sender_row_meta("U0ALICE", "T0TEAM", False) == {
        SLACK_SENDER_META_KEY: {"team_id": "T0TEAM", "enterprise_id": "ETEST", "user_id": "U0ALICE"}
    }


def test_a_known_author_is_kept_and_an_unknown_one_replaced():
    tailnet = Principal(
        id=principal_id_for("tailscale", "tailnet", "a@x.test"),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject="a@x.test",
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name="a"),
    )
    kept = {**_row(ALICE_IDS), MESSAGE_AUTHOR_META_KEY: tailnet.to_public_dict()}
    assert si.stamp_sender_author(kept) is None
    assert _author(kept).id == tailnet.id
    unknown = {**_row(ALICE_IDS), MESSAGE_AUTHOR_META_KEY: Principal.unknown().to_public_dict()}
    assert si.stamp_sender_author(unknown) is not None
    assert _author(unknown).subject == "" and _author(unknown).via == "slack"


def test_the_public_author_says_via_slack_and_hides_the_ids():
    meta = _row(ALICE_IDS)
    si.stamp_sender_author(meta)
    public = meta[MESSAGE_AUTHOR_META_KEY]
    # The raw ids are consumed by the reader; only the public author remains.
    assert SLACK_SENDER_META_KEY not in meta
    assert public["via"] == "slack"
    assert public["display"]["name"] == "Alice Example"
    assert "U0ALICE" not in json.dumps(public) and "T0TEAM" not in json.dumps(public)
    # Read back from the public shape, it is still a channel principal.
    assert is_channel_principal(principal_or_unknown(public))


# -- the transcript door: ConversationLog.append -------------------------------------------


def test_transcript_append_stamps_the_author_and_the_first_rows_creator(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("slack:1.1", "user", "hi", extra_meta=_row(ALICE_IDS))
    log.append("slack:1.1", "assistant", "hello")
    log.append("slack:1.1", "user", "me too", extra_meta=_row(BOB_IDS))
    rows = log.read_messages("slack:1.1")
    assert _author(rows[0]["meta"]).display.name == "Alice Example"
    assert _author(rows[2]["meta"]).display.name == "Bob Example"
    assert "meta" not in rows[1] or MESSAGE_AUTHOR_META_KEY not in rows[1]["meta"]
    stored = SessionAttribution.from_metadata(log.get_metadata("slack:1.1"))
    assert stored.availability is AttributionAvailability.RECORDED
    # The first sender started it; the second never replaces them.
    assert stored.creator.subject == "U0ALICE"
    # The raw Slack ids never reach the stored transcript.
    assert "U0ALICE" not in log._path("slack:1.1").read_text(encoding="utf-8").split("\n", 1)[1]


def test_a_notice_before_the_first_person_does_not_hide_the_creator(tmp_path):
    """A mention in an existing thread records the thread's parent as a notice
    row before the person's own row: they still started the session."""
    log = ConversationLog(base_dir=tmp_path)
    log.append("slack:1.2", "notice", "Thread started by someone else: ...")
    log.append("slack:1.2", "user", "hi", extra_meta=_row(ALICE_IDS))
    stored = SessionAttribution.from_metadata(log.get_metadata("slack:1.2"))
    assert stored.availability is AttributionAvailability.RECORDED
    assert stored.creator.subject == "U0ALICE"


def test_a_session_with_earlier_rows_is_never_back_filled(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("slack:2.2", "user", "before attribution")
    log.append("slack:2.2", "user", "later", extra_meta=_row(ALICE_IDS))
    assert SESSION_ATTRIBUTION_META_KEY not in log.get_metadata("slack:2.2")
    assert SessionAttribution.from_metadata(log.get_metadata("slack:2.2")).availability is (
        AttributionAvailability.LEGACY_MISSING
    )


def test_a_malformed_sender_leaves_the_transcript_row_unattributed(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    bad = {SLACK_SENDER_META_KEY: {"team_id": "nope", "user_id": "U0ALICE"}}
    log.append("slack:3.3", "user", "hi", extra_meta=bad)
    row = log.read_messages("slack:3.3")[0]
    assert MESSAGE_AUTHOR_META_KEY not in (row.get("meta") or {})
    assert SLACK_SENDER_META_KEY not in (row.get("meta") or {})
    assert SESSION_ATTRIBUTION_META_KEY not in log.get_metadata("slack:3.3")


def test_an_assistant_row_never_reads_a_sender(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("slack:4.4", "assistant", "x", extra_meta=_row(ALICE_IDS))
    row = log.read_messages("slack:4.4")[0]
    assert MESSAGE_AUTHOR_META_KEY not in (row.get("meta") or {})
    assert SLACK_SENDER_META_KEY not in (row.get("meta") or {})


def test_the_native_turn_save_writes_the_sender(tmp_path):
    from kiro_crew.llm_helpers import save_conversation_turn_off_loop

    log = ConversationLog(base_dir=tmp_path)
    asyncio.run(
        save_conversation_turn_off_loop(
            log, "slack:5.5", "hi", "hello", source_user="U0ALICE", user_meta=_row(ALICE_IDS)
        )
    )
    user = log.read_messages("slack:5.5")[0]
    assert _author(user["meta"]).subject == "" and _author(user["meta"]).via == "slack"
    assert SessionAttribution.from_metadata(log.get_metadata("slack:5.5")).creator.subject == (
        "U0ALICE"
    )


def test_a_surfaced_channel_session_reads_its_recorded_creator(tmp_path, monkeypatch):
    from kiro_crew.dashboard import channel_slots

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log
    log.append("slack:6.6", "user", "hi", extra_meta=_row(ALICE_IDS))
    state.sessions.channel_key_for_stem = lambda stem: ""
    slot = channel_slots.surface_channel_session(
        state,
        {"key": "slack_6.6", "title": "", "modified": 1.0},
        log.get_metadata("slack:6.6"),
        log.read_messages("slack:6.6"),
        session_key="slack:6.6",
    )
    assert slot is not None
    public = attr.public_attribution_of_slot(slot)
    assert public["availability"] == "recorded"
    assert public["creator"]["display"]["name"] == "Alice Example"
    assert public["creator"]["via"] == "slack"


# -- the window door: _ChatSlot.append -----------------------------------------------------


def test_window_append_stamps_the_author_and_a_fresh_slots_creator():
    slot = _ChatSlot("bound-1")
    row = slot.append("user", "hi", "msg msg-u", meta=_row(ALICE_IDS))
    assert _author(row["meta"]).display.name == "Alice Example"
    stamped = attr.slot_attribution(slot)
    assert stamped is not None and stamped.creator.subject == "U0ALICE"
    slot.append("user", "me too", "msg msg-u", meta=_row(BOB_IDS))
    assert attr.slot_attribution(slot).creator.subject == "U0ALICE"


def test_window_append_never_makes_a_later_slack_sender_the_creator():
    slot = _ChatSlot("bound-2")
    slot.append("user", "typed on the dashboard", "msg msg-u")
    slot.append("user", "from slack", "msg msg-u", meta=_row(ALICE_IDS))
    assert attr.slot_attribution(slot) is None


def test_window_append_of_a_malformed_sender_changes_nothing():
    slot = _ChatSlot("bound-3")
    row = slot.append("user", "hi", "msg msg-u", meta={SLACK_SENDER_META_KEY: {"x": 1}})
    assert MESSAGE_AUTHOR_META_KEY not in (row.get("meta") or {})
    assert SLACK_SENDER_META_KEY not in (row.get("meta") or {})
    assert attr.slot_attribution(slot) is None


def test_window_rows_never_carry_the_raw_ids():
    slot = _ChatSlot("bound-4")
    user = slot.append("user", "hi", "msg msg-u", meta=_row(ALICE_IDS))
    replayed = slot.append("user", "old", "msg msg-u", meta=_row(BOB_IDS), broadcast=False)
    other = slot.append("assistant", "ok", "msg msg-a", meta=_row(BOB_IDS))
    for row in (user, replayed, other):
        assert SLACK_SENDER_META_KEY not in (row.get("meta") or {})
    # Only a live user row is stamped.
    assert MESSAGE_AUTHOR_META_KEY not in (replayed.get("meta") or {})
    assert MESSAGE_AUTHOR_META_KEY not in (other.get("meta") or {})


def test_window_append_with_rows_beyond_the_window_records_no_creator():
    slot = _ChatSlot("bound-5")
    slot.total_messages = 40  # 40 rows on disk, none of them in this window
    slot.append("user", "hi", "msg msg-u", meta=_row(ALICE_IDS))
    assert attr.slot_attribution(slot) is None


def test_a_request_handler_task_can_never_speak_as_a_slack_person():
    from kiro_crew.shared_crew.attribution import bind_request_author

    tailnet = ActorContext(
        principal=Principal(
            id=principal_id_for("tailscale", "tailnet", "bob@x.test"),
            kind=PrincipalKind.HUMAN,
            provider="tailscale",
            issuer="tailnet",
            subject="bob@x.test",
            provenance=Provenance.TAILSCALE_SERVE,
            display=DisplaySnapshot(name="bob"),
        ),
        request_id="r",
        transport=Transport.HTTP,
    )

    async def go():
        bind_request_author(tailnet)
        return _ChatSlot("bound-6").append("user", "hi", "msg msg-u", meta=_row(ALICE_IDS))

    row = asyncio.run(go())
    assert _author(row["meta"]).id == tailnet.principal.id
    assert SLACK_SENDER_META_KEY not in row["meta"]


def test_an_edited_queued_slack_message_loses_its_sender():
    slot = _ChatSlot("bound-7")
    qid = slot.queue_append("retry it", meta=_row(ALICE_IDS), directive_user_origin=True)
    assert slot._queue_repository.queue_edit_by_id(slot, qid, "delete everything")
    entry = next(item for item in slot._queue if item["id"] == qid)
    assert SLACK_SENDER_META_KEY not in (entry.get("meta") or {})
    unchanged = slot.queue_append("as sent", meta=_row(BOB_IDS), directive_user_origin=True)
    assert slot._queue_repository.queue_edit_by_id(slot, unchanged, "as sent")
    entry = next(item for item in slot._queue if item["id"] == unchanged)
    assert entry["meta"][SLACK_SENDER_META_KEY]["user_id"] == "U0BOB"


# -- attribution only, never access ---------------------------------------------------------


def test_a_slack_principal_is_not_a_collaborator():
    p = si.sender_principal(ALICE_IDS)
    assert p is not None
    assert not is_collaborator_actor(si.slack_actor(p))
    assert si.slack_actor(p).transport is Transport.CHANNEL


def test_slack_event_provenance_is_no_membership_boundary():
    """Only an enforced membership boundary admits a collaborator; Slack's is not one.

    A Slack person on a dashboard-user socket (the Slack-allowlist link path)
    stays a non-collaborator: the actor's provenance, not its kind, decides.
    """
    from kiro_crew.shared_crew import collaborators

    assert Provenance.SLACK_EVENT not in collaborators._MEMBER_PROVENANCES
    p = si.sender_principal(ALICE_IDS)
    for transport in (Transport.HTTP, Transport.WEBSOCKET, Transport.CHANNEL):
        actor = ActorContext(principal=p, request_id="r", transport=transport)
        assert not is_collaborator_actor(actor)


def test_slack_people_are_no_evidence_of_a_team_path():
    from kiro_crew.shared_crew.crew_mode import others_observed
    from kiro_crew.shared_crew.identity import local_operator_principal

    viewer = local_operator_principal("owner")
    slack = si.sender_principal(ALICE_IDS)
    public = principal_or_unknown(slack.to_public_dict())
    assert not others_observed(viewer, [slack], creators=[public])


def test_a_slack_author_owns_no_admission_budget():
    from kiro_crew.shared_crew.turn_gate import queued_initiator

    meta = _row(ALICE_IDS)
    si.stamp_sender_author(meta)
    assert queued_initiator([{"meta": meta}]) == ""


def test_a_slack_writer_does_not_make_a_personal_crew_shared(monkeypatch):
    from kiro_crew.shared_crew import memory_changes

    monkeypatch.setattr("kiro_crew.shared_crew.access.setup_paths_dict", lambda: {})
    slack = si.sender_principal(ALICE_IDS)
    assert not memory_changes.crew_is_shared(SimpleNamespace(owner_id=""), slack)


def test_a_request_may_not_supply_the_sender_key():
    from kiro_crew.dashboard.chat_handlers import RESERVED_ROW_META_KEYS

    assert SLACK_SENDER_META_KEY in RESERVED_ROW_META_KEYS


# -- decisions answered in Slack -----------------------------------------------------------


def _decision_state() -> SimpleNamespace:
    return SimpleNamespace(
        _approval_futures={},
        _pending_approvals={},
        _slots={},
        broadcast_ws=MagicMock(),
        broadcast_ws_owners=MagicMock(),
        _log=MagicMock(),
    )


def test_a_slack_click_on_a_dashboard_approval_names_the_clicker():
    from kiro_crew.shared_crew.decisions import (
        approval_decision_id,
        decisions_for,
        resolve_channel_click,
    )

    async def go():
        state = _decision_state()
        fut = asyncio.get_running_loop().create_future()
        slot = SimpleNamespace(key="bound-9", _approval_futures={"req-1": fut}, messages=[])
        state._slots = {"bound-9": slot}
        clicker = si.sender_principal(BOB_IDS)
        settled = resolve_channel_click(
            state, "req-1", True, clicker, lambda: (fut.set_result("approved"), True)[1]
        )
        decision = decisions_for(state).get(approval_decision_id("bound-9", "req-1"))
        return settled, decision, fut.result()

    settled, decision, result = asyncio.run(go())
    assert settled is True and result == "approved"
    assert decision.state is DecisionState.RESOLVED
    assert decision.responder.subject == "U0BOB"


def test_a_slack_click_with_no_live_approval_is_the_old_settle_alone():
    from kiro_crew.shared_crew.decisions import resolve_channel_click

    calls = []
    out = resolve_channel_click(
        _decision_state(), "req-x", False, si.sender_principal(BOB_IDS), lambda: calls.append(1)
    )
    assert calls == [1] and out is False


def test_a_slack_sessions_own_approval_is_recorded_asked_and_answered():
    from kiro_crew.shared_crew.decisions import (
        decisions_for,
        open_channel_approval,
        settle_channel_approval,
    )

    state = _decision_state()
    asker = si.sender_principal(ALICE_IDS)
    answered = open_channel_approval(state, "slack_7_7", "r1", asker)
    settle_channel_approval(state, answered, "approved", si.sender_principal(BOB_IDS))
    d = decisions_for(state).get(answered)
    assert d.state is DecisionState.RESOLVED
    assert d.initiator.subject == "U0ALICE" and d.responder.subject == "U0BOB"
    expired = open_channel_approval(state, "slack_7_7", "r2", asker)
    settle_channel_approval(state, expired, None, None)
    assert decisions_for(state).get(expired).state is DecisionState.EXPIRED


@pytest.mark.asyncio
async def test_handle_interaction_records_who_clicked():
    from kiro_crew.slack import handler

    provider = MagicMock()
    provider.approve_tool = AsyncMock(return_value=True)
    pending = handler._PendingApproval(provider, "req-2", "slack:8.8")
    handler._pending_approvals["C1:111.1"] = pending
    try:
        with patch.object(handler, "is_allowed_user", return_value=True):
            await handler.handle_interaction(
                "C1", "111.1", "approve_tool", user_id="U0BOB", team_id="T0TEAM"
            )
    finally:
        handler._pending_approvals.pop("C1:111.1", None)
    assert pending.future.result() == "approved"
    assert pending.responder.subject == "U0BOB"
    assert pending.responder.kind is PrincipalKind.HUMAN


# -- the linked-thread route (and the code-channel route that shares it) ---------------------


async def _route(tmp_path, monkeypatch, route) -> dict:
    from kiro_crew.slack import handler

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    slot = state.get_or_create_slot("bound-slot")
    monkeypatch.setattr(state, "get_linked_slot", lambda _ts: slot, raising=False)
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
    ):
        assert await route(handler) is True
    return [m for m in slot.messages if m.get("role") == "user"][-1]


@pytest.mark.asyncio
async def test_a_linked_thread_message_carries_its_slack_byline(tmp_path, monkeypatch):
    async def route(handler):
        return await handler.maybe_route_linked_thread(
            "hello", "slack:9.9", "U0ALICE", "C1", MagicMock(), "9.9", team_id="TTEST"
        )

    row = await _route(tmp_path, monkeypatch, route)
    author = _author(row["meta"])
    assert author.kind is PrincipalKind.HUMAN and author.via == "slack"
    assert author.display.name == "Alice Example"
    assert author.id == principal_id_for("slack", "ETEST", "U0ALICE")


@pytest.mark.asyncio
async def test_the_code_channel_call_shape_is_attributed_without_a_team(tmp_path, monkeypatch):
    """The Slack Code stack's ``routing._route_to_slot`` calls the route with six
    positional arguments and no team. That exact call, with the gateway's validated
    workspace as the scope, must already carry the byline (runs on every tree)."""

    async def route(handler):
        return await handler.maybe_route_linked_thread(
            "fix the build", "bound-slot", "U0ALICE", "C0CODE", MagicMock(), "10.10"
        )

    row = await _route(tmp_path, monkeypatch, route)
    assert _author(row["meta"]).id == principal_id_for("slack", "ETEST", "U0ALICE")


@pytest.mark.asyncio
async def test_a_code_channel_message_carries_its_slack_byline(tmp_path, monkeypatch):
    """(b) Code channels route into their bound session through the same seam.

    Skips until the Slack Code stack (``kiro_crew.slack.code_channels``) is on
    the same tree as Shared Crew, then runs: a message routed into the bound
    session must carry its Slack sender's byline.
    """
    routing = pytest.importorskip("kiro_crew.slack.code_channels.routing")
    monkeypatch.setattr(routing, "_adopt_temps", lambda _key: None, raising=False)
    binding = SimpleNamespace(
        channel_id="C0CODE", team_id="TTEST", session_key="bound-slot", anchor_ts="10.10"
    )

    async def route(handler):
        orch = SimpleNamespace(slack=MagicMock())
        return await routing._route_to_slot(orch, binding, "fix the build", "U0ALICE")

    row = await _route(tmp_path, monkeypatch, route)
    author = _author(row["meta"])
    assert author.kind is PrincipalKind.HUMAN and author.via == "slack"
    assert author.id == principal_id_for("slack", "ETEST", "U0ALICE")


@pytest.mark.asyncio
async def test_a_trusted_bots_linked_message_stays_unattributed(tmp_path, monkeypatch):
    async def route(handler):
        return await handler.maybe_route_linked_thread(
            "beep", "slack:11.11", "U0ALICE", "C1", MagicMock(), "11.11", sender_is_bot=True
        )

    row = await _route(tmp_path, monkeypatch, route)
    assert MESSAGE_AUTHOR_META_KEY not in (row.get("meta") or {})


def test_slack_mirror_rows_name_the_sender_not_unknown(monkeypatch):
    from kiro_crew.slack.handler_runtime import finalize

    slot = _ChatSlot("mirror-slot")
    slot._on_message = None
    ds = SimpleNamespace(_slots={"mirror-slot": slot}, push_slots_update=MagicMock())
    monkeypatch.setattr("kiro_crew.slack.handler._dashboard_state", ds)
    finalize._mirror_to_dashboard("dashboard:mirror-slot", "hi", "hello", _row(ALICE_IDS))
    user = [m for m in slot.messages if m["role"] == "user"][-1]
    assert _author(user["meta"]).display.name == "Alice Example"


# -- a Slack message queued behind a busy turn ---------------------------------------------


async def _drain(tmp_path, monkeypatch, *senders) -> dict:
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.subagents = None
    slot = state.get_or_create_slot("busy-bound")
    for n, ids in enumerate(senders):
        slot.queue_append(f"queued {n}", meta=_row(ids), directive_user_origin=True)
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    return [m for m in slot.messages if m.get("role") == "user"][-1]


@pytest.mark.asyncio
async def test_a_queued_slack_message_drains_with_its_byline(tmp_path, monkeypatch):
    row = await _drain(tmp_path, monkeypatch, ALICE_IDS)
    assert _author(row["meta"]).display.name == "Alice Example"


def test_a_slack_click_on_a_background_approval_names_the_clicker():
    """The automation approval a cron or sub-agent raised, answered in Slack."""
    from kiro_crew.shared_crew.decisions import (
        decisions_for,
        resolve_channel_click,
        state_approval_decision_id,
    )

    async def go():
        state = _decision_state()
        fut = asyncio.get_running_loop().create_future()
        state._approval_futures = {"bg-1": fut}
        state._pending_approvals = {"bg-1": {"slot": "", "source": "cron:nightly"}}
        settled = resolve_channel_click(
            state,
            "bg-1",
            False,
            si.sender_principal(BOB_IDS),
            lambda: (fut.set_result(False), True)[1],
        )
        return settled, decisions_for(state).get(state_approval_decision_id("bg-1"))

    settled, decision = asyncio.run(go())
    assert settled is True
    assert decision.outcome == "rejected"
    assert decision.responder.subject == "U0BOB"
    assert decision.initiator.kind is PrincipalKind.SYSTEM


def test_a_cancelled_slack_approval_ends_withdrawn():
    from kiro_crew.shared_crew.decisions import (
        decisions_for,
        open_channel_approval,
        settle_channel_approval,
    )

    state = _decision_state()
    d = open_channel_approval(state, "slack_7_8", "r9", si.sender_principal(ALICE_IDS))
    settle_channel_approval(state, d, None, None, withdrawn=True)
    assert decisions_for(state).get(d).state is DecisionState.WITHDRAWN


def test_a_temporary_slack_thread_announces_no_decision(monkeypatch):
    from kiro_crew.slack import handler

    state = _decision_state()
    monkeypatch.setattr(handler, "_dashboard_state", state)
    monkeypatch.setattr(handler, "_is_slack_restricted", lambda _key: True)
    assert handler._open_native_decision("slack:9.1", "r1", _row(ALICE_IDS)) is None
    state.broadcast_ws.assert_not_called()
    monkeypatch.setattr(handler, "_is_slack_restricted", lambda _key: False)
    assert handler._open_native_decision("slack:9.1", "r1", _row(ALICE_IDS)) is not None


def test_a_failing_attribution_still_settles_the_click(monkeypatch):
    from kiro_crew.shared_crew import decisions
    from kiro_crew.slack import handler

    def boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(decisions, "resolve_channel_click", boom)
    state = MagicMock()
    state.resolve_approval.return_value = True
    assert handler._resolve_dashboard_approval(state, "req-5", True, si.sender_principal(BOB_IDS))
    state.resolve_approval.assert_called_once_with("req-5", True)
