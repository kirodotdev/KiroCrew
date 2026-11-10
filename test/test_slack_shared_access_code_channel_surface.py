"""slack.shared_access vs the code-channel surface (agent tools, commands, wrap-up/archive).

A shared user drives work as the owner in DMs, mentions and threads, but code channels
stay the owner's. Each entry point the code-channel surface adds must refuse them:
the channel's slash commands, the wrap-up's Archive button (at the interactions gate
and in its own handler), and a thread linked to a code channel's session from
anywhere, whose turn would reach the code-channel tools.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    CH,
    SLOT,
    _bind,
    _calls,
    _click,
    _slash,
    _wrap,
    env_fixture,
)

# Imported here, never under a patch: a module first imported while a name is
# patched binds the patched value for the rest of the session.
from kiro_crew.slack import events  # noqa: F401
from kiro_crew.slack.agent_sessions.session_events import _shared_user_in_code_channel
from kiro_crew.slack.code_channels import commands, wrapup
from kiro_crew.slack.code_channels.routing import shared_user_refused_for_slot
from kiro_crew.slack.handler_runtime.inbound import (
    LinkedRoute,
    route_linked_thread,
    shared_user_refused_for_session,
)
from kiro_crew.slack.shared_access import shared_action_permitted

SHARED = "U0SHARED01"


def _as_shared():
    """Treat SHARED as a confirmed, listed shared user (never the owner)."""
    return (
        patch("kiro_crew.slack.handler.is_shared_user", lambda u: u == SHARED),
        patch("kiro_crew.slack.handler.is_shared_listed", lambda u: u == SHARED),
    )


def test_a_shared_user_runs_no_code_channel_command(env: Any) -> None:
    _bind(env)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    a, b = _as_shared()
    with a, b:
        for i, command in enumerate(("/summarize", "/wrap-up", "/create-pr")):
            slash = _slash(command, user=SHARED, trigger=f"T-{i}")
            assert asyncio.run(commands.handle_slash(env.orch, slash, respond))
    assert env.routed == [] and len(replies) == 3
    assert all(r["outcome"] == "denied" for r in env.sel.ops("slack.code_channel.command"))


def test_a_shared_user_cannot_archive_a_code_channel(env: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    # Refused at the interactions gate: Archive is not a button a shared user may press.
    assert shared_action_permitted(wrapup.ARCHIVE_ACTION_ID) is False
    a, b = _as_shared()
    with a, b:
        # And by the handler itself, which admits the owner only.
        assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(SHARED, ts)))
    assert _calls(env, "agents.conversations.archive") == []
    assert env.sel.ops("slack.code_channel.archive")[-1]["outcome"] == "denied"


@pytest.mark.parametrize("how", ["slot", "anchor", "revoked_anchor", "unrelated"])
def test_a_linked_thread_into_a_code_channel_session_is_refused(env: Any, how: str) -> None:
    _bind(env, anchor="100.000001")
    if how == "revoked_anchor":
        env.store.drop_binding(CH)
    slot, thread = {
        "slot": (SLOT, "555.000001"),
        "anchor": ("dashboard:chat-other", "100.000001"),
        "revoked_anchor": ("dashboard:chat-other", "100.000001"),
        "unrelated": ("dashboard:chat-other", "555.000001"),
    }[how]
    assert shared_user_refused_for_slot(slot, thread) is (how != "unrelated")


def test_route_linked_thread_refuses_a_shared_user_into_a_code_channel_session(
    env: Any,
) -> None:
    from types import SimpleNamespace

    from kiro_crew.slack import handler

    _bind(env)
    state = SimpleNamespace(get_linked_slot=lambda ts: SimpleNamespace(key=SLOT))
    a, b = _as_shared()
    with a, b, patch.object(handler, "_dashboard_state", state):
        # A plain DM thread the owner linked to the code channel's dashboard session.
        outcome = asyncio.run(
            route_linked_thread(
                "now add a regression test",
                "slack:555.000001",
                SHARED,
                "D0DM00001",
                env.slack,
                "555.000001",
            )
        )
    assert outcome is LinkedRoute.REFUSED


def test_a_closed_tab_still_refuses_a_shared_users_linked_thread(env: Any) -> None:
    # The code channel's session has no live slot (its tab is closed), but the
    # thread index still names it as the thread's owner.
    from types import SimpleNamespace

    from kiro_crew.slack import handler

    _bind(env)
    sessions = SimpleNamespace(get_session_for_thread=lambda ts: SLOT)
    state = SimpleNamespace(get_linked_slot=lambda ts: None, sessions=sessions)
    a, b = _as_shared()
    with a, b, patch.object(handler, "_dashboard_state", state):
        outcome = asyncio.run(
            route_linked_thread(
                "now add a regression test",
                "slack:555.000001",
                SHARED,
                "D0DM00001",
                env.slack,
                "555.000001",
            )
        )
    assert outcome is LinkedRoute.REFUSED


def test_a_pinned_answer_to_a_code_channel_session_is_refused(env: Any) -> None:
    _bind(env)
    a, b = _as_shared()
    with a, b:
        assert asyncio.run(shared_user_refused_for_session(SHARED, SLOT, "", "D0DM00001"))
        assert not asyncio.run(
            shared_user_refused_for_session(SHARED, "dashboard:chat-other", "", "D0DM00001")
        )
    # The owner is never refused here.
    assert not asyncio.run(shared_user_refused_for_session("UOWNER", SLOT, "", "D0DM00001"))


def test_a_shared_users_rename_or_stop_in_a_code_channel_thread_is_refused(env: Any) -> None:
    # The agent-session rename and thread Stop: the thread's session is a code
    # channel's, or the thread is its (bound or revoked) anchor, with Slack Code off.
    _bind(env, anchor="100.000001")
    env.on = False
    a, b = _as_shared()
    with a, b:
        assert asyncio.run(_shared_user_in_code_channel(SHARED, "D0DM00001", "555.0", SLOT))
        assert asyncio.run(_shared_user_in_code_channel(SHARED, "C0ORIGIN", "100.000001"))
        env.store.drop_binding(CH)
        assert asyncio.run(_shared_user_in_code_channel(SHARED, "C0ORIGIN", "100.000001"))
        assert not asyncio.run(_shared_user_in_code_channel(SHARED, "C0ORIGIN", "555.000001"))


@pytest.mark.parametrize(
    "spelling", [SLOT, f"dashboard:{SLOT}", "slack:100.000001", "dashboard:slack:100.000001"]
)
def test_every_spelling_of_a_code_channel_session_is_refused(env: Any, spelling: str) -> None:
    # The binding stores the bare slot key; the thread index and an asker carry the
    # ``dashboard:`` form, and a channel-born slot runs on ``slack:<anchor>``.
    _bind(env, anchor="100.000001")
    assert shared_user_refused_for_slot(spelling, "") is True
    assert shared_user_refused_for_slot("dashboard:chat-other", "") is False


def test_a_refused_answer_to_a_code_channel_session_leaves_the_card_answerable(
    env: Any,
) -> None:
    # A card posted by the code channel's session, mirrored into an ordinary thread:
    # the shared user's ordinary answer is refused BEFORE the card is claimed.
    from types import SimpleNamespace

    from kiro_crew.slack import interactions, interactions_options, outbound

    _bind(env)
    sessions = SimpleNamespace(get_session_for_thread=lambda ts: f"dashboard:{SLOT}")
    orch = SimpleNamespace(slack=env.slack, dashboard_state=SimpleNamespace(sessions=sessions))
    told: list[str] = []

    async def _tell(channel: str, user: str, thread: str, text: str) -> None:
        told.append(text)

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
        patch.object(interactions, "_tell_user", _tell),
        patch.object(outbound, "_ANSWERED", {}),
    ):
        refused = asyncio.run(
            interactions_options._shared_destination_refused(
                "C0PLAIN", SHARED, "555.000001", "options", None
            )
        )
        assert refused is True
        assert outbound._ANSWERED == {}  # nothing claimed: the owner can still answer
    assert told == ["⛔ Only the bot owner can do that."]


@pytest.mark.parametrize("kind, refused", [("OURS", True), ("ORDINARY", False), ("UNKNOWN", False)])
def test_a_card_answer_in_an_unbound_code_channel_of_ours_is_refused(
    env: Any, kind: str, refused: bool
) -> None:
    # Slack's UI opened the code channel and nothing bound it yet, with Slack Code on:
    # a typed message is refused there (inbound_gate), so a card answer is too.
    from types import SimpleNamespace

    from kiro_crew.slack import interactions, interactions_options
    from kiro_crew.slack.code_channels import recognition, routing

    async def classify(*_a: Any) -> Any:
        return getattr(recognition.ChannelKind, kind), None

    orch = SimpleNamespace(slack=env.slack, dashboard_state=None)
    told: list[str] = []

    async def _tell(channel: str, user: str, thread: str, text: str) -> None:
        told.append(text)

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(routing.recognition, "classify_channel", classify),
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
        patch.object(interactions, "_tell_user", _tell),
    ):
        assert (
            asyncio.run(
                interactions_options._shared_destination_refused(
                    "C0UNBOUND", SHARED, "555.000001", "options", None
                )
            )
            is refused
        )
    assert bool(told) is refused


def test_a_shared_users_stop_in_a_thread_resolving_to_a_code_channel_session_is_refused(
    env: Any,
) -> None:
    # The Stop's thread is neither the code channel nor its anchor, but the session
    # the Stop resolves to is the code channel's (a dashboard-linked thread).
    from types import SimpleNamespace

    from kiro_crew.slack.agent_sessions import session_events

    _bind(env, anchor="100.000001")
    stops: list[str] = []

    async def stop_turn(key: str, **_kw: Any) -> str:
        stops.append(key)
        return "soft"

    async def _prime(*_a: Any) -> None:
        return None

    orch = SimpleNamespace(sessions=SimpleNamespace(stop_turn=stop_turn), _session_tasks={})
    event = {
        "channel": "D0DM00001",
        "thread_ts": "555.000001",
        "user": SHARED,
        "event_ts": "555.900001",
    }
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(session_events, "prime_shared_user", _prime),
        patch.object(session_events, "_may_use_bot", lambda u: True),
        patch.object(
            session_events,
            "resolve_session",
            lambda *_a: ("555.000001", f"dashboard:{SLOT}", False),
        ),
        patch.object(session_events, "_is_ours", lambda *_a: True),
    ):
        outcome = asyncio.run(session_events.handle_stopped(orch, event))
    assert outcome == "denied" and stops == []


def test_a_shared_users_bang_stop_in_a_code_channel_sessions_thread_is_refused(
    env: Any,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from slack_events_coverage_helpers import _event, _make_orch

    _bind(env)
    orch = _make_orch()
    orch.sessions.has_session = MagicMock(return_value=True)
    orch.sessions.get_session_for_thread = MagicMock(return_value=f"dashboard:{SLOT}")
    orch.sessions.note_stop = MagicMock(return_value=True)
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    a, b = _as_shared()
    with (
        a,
        b,
        patch("kiro_crew.slack.events.sel", return_value=MagicMock()),
        patch("kiro_crew.slack.events.is_owner", return_value=False),
        patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
        patch("kiro_crew.slack.events.is_shared_user", lambda u: u == SHARED),
        patch("kiro_crew.slack.events.is_shared_listed", lambda u: u == SHARED),
    ):
        asyncio.run(
            events._route_message(
                orch,
                _event(text="!stop", user=SHARED, channel="D0DM00001", thread_ts="555.000001"),
                events.SeenCache(),
            )
        )
    orch.sessions.stop_turn.assert_not_awaited()
    orch.sessions.note_stop.assert_not_called()


def test_a_native_turn_relinked_to_a_code_channel_session_is_not_acquired(env: Any) -> None:
    # The owner links the thread to the code channel's session after the linked-thread
    # check ran: the turn re-resolves its owner before acquisition and must refuse.
    from slack_handler_helpers import FakeSessionManager

    from conftest import MockSlackClient
    from kiro_crew.slack import handler

    _bind(env)

    class Relinked(FakeSessionManager):
        owner_key = "dashboard:chat-other"

        def get_session_for_thread(self, _ts: str) -> str:
            return self.owner_key

    sessions = Relinked()

    async def resolve_memory(_builder: Any, key: str) -> str:
        if key == "dashboard:chat-other":
            await asyncio.sleep(0)
            sessions.owner_key = f"dashboard:{SLOT}"
        return f"memory:{key}"

    async def hydrate(*_a: Any) -> None:
        return None

    slack = MockSlackClient()
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(handler, "session_store_for_turn", resolve_memory),
        patch.object(handler, "_hydrate_thread_overrides", hydrate),
        patch.object(handler, "_hydrate_conv_flags", lambda *_a: None),
    ):
        asyncio.run(
            handler.handle_message(slack, sessions, "D0DM00001", "hi", "555.000001", "msg1", SHARED)
        )
    assert sessions.keys_seen == []
    # The refused turn takes down its Working block (with its Stop button) and status.
    working = [p["ts"] for kind, p in slack.actions if kind == "blocks"]
    deleted = {p["ts"] for kind, p in slack.actions if kind == "delete"}
    assert working and set(working) <= deleted
    statuses = [p["status"] for kind, p in slack.actions if kind == "set_thread_status"]
    assert not statuses or statuses[-1] == ""


def test_a_transport_turn_relinked_to_a_code_channel_session_is_not_acquired(
    env: Any,
) -> None:
    import importlib

    from kiro_crew.slack import transport_dispatch

    golden = importlib.import_module("test_slack_golden_transcript")
    _bind(env)
    acquired: list[str] = []

    class Relinked(golden.FakeSessions):  # type: ignore[name-defined, misc]
        owner_key = "dashboard:chat-other"

        def get_session_for_thread(self, _ts: str) -> str:
            return self.owner_key

        async def get_or_create(self, session_key: str, **_kw: Any) -> Any:
            acquired.append(session_key)
            return await super().get_or_create(session_key)

    sessions = Relinked(golden.ScriptedProvider([golden.make_event("ok")]))

    async def resolve_memory(_builder: Any, key: str) -> str:
        if key == "dashboard:chat-other":
            sessions.owner_key = f"dashboard:{SLOT}"
        return f"memory:{key}"

    async def hydrate(*_a: Any) -> None:
        return None

    slack = golden.RecordingSlackClient()
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(transport_dispatch, "session_store_for_turn", resolve_memory),
        patch.object(transport_dispatch, "_hydrate_thread_overrides", hydrate),
        patch.object(transport_dispatch, "_hydrate_conv_flags", lambda *_a: None),
        patch.object(transport_dispatch, "_get_default_agent", lambda: ""),
        patch.object(transport_dispatch, "_thread_agents", {}),
    ):
        asyncio.run(
            transport_dispatch.handle_message_transport(
                slack,
                sessions,
                "D0DM00001",
                "hi",
                "555.000001",
                "555.000002",
                SHARED,
            )
        )
    assert acquired == []
    # The renderer's teardown clears the working status the refused turn set.
    statuses = [kw.get("status") for m, kw in slack.transcript if m == "set_thread_status"]
    assert not statuses or statuses[-1] == ""
    assert not [m for m, _kw in slack.transcript if m == "post_blocks"]


@pytest.mark.parametrize("kind, refused", [("OURS", True), ("ORDINARY", False)])
def test_a_stop_or_rename_in_an_unbound_code_channel_of_ours_is_refused(
    env: Any, kind: str, refused: bool
) -> None:
    # The inline Stop and the agent-session Stop/rename meet the same unbound-channel
    # rule as a typed message and a card answer (Slack Code on, nothing bound yet).
    from types import SimpleNamespace

    from kiro_crew.slack import interactions, interactions_sessions
    from kiro_crew.slack.code_channels import recognition, routing

    async def classify(*_a: Any) -> Any:
        return getattr(recognition.ChannelKind, kind), None

    orch = SimpleNamespace(slack=env.slack)
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(routing.recognition, "classify_channel", classify),
        patch.object(interactions, "_orch", orch),
    ):
        assert asyncio.run(interactions_sessions._outside_code_channel("C0UNBOUND")) is not refused
        assert (
            asyncio.run(_shared_user_in_code_channel(SHARED, "C0UNBOUND", "555.0", orch=orch))
            is refused
        )


@pytest.mark.parametrize("kind, refused", [("OURS", True), ("ORDINARY", False)])
def test_a_redispatched_turn_in_an_unbound_code_channel_of_ours_is_refused(
    env: Any, kind: str, refused: bool
) -> None:
    # A review revision or a queued message reaches handle_message without
    # inbound_gate: the shared chokepoint refuses the unbound code channel too.
    from types import SimpleNamespace

    from kiro_crew.slack import handler
    from kiro_crew.slack.code_channels import recognition, routing

    async def classify(*_a: Any) -> Any:
        return getattr(recognition.ChannelKind, kind), None

    state = SimpleNamespace(get_linked_slot=lambda ts: None, sessions=None)
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(routing.recognition, "classify_channel", classify),
        patch.object(handler, "_dashboard_state", state),
    ):
        outcome = asyncio.run(
            route_linked_thread(
                "revise it", "slack:555.000001", SHARED, "C0UNBOUND", env.slack, "555.000001"
            )
        )
    assert (outcome is LinkedRoute.REFUSED) is refused


@pytest.mark.parametrize("kind, refused", [("OURS", True), ("ORDINARY", False)])
def test_no_button_or_draft_edit_of_a_shared_user_acts_in_an_unbound_code_channel(
    env: Any, kind: str, refused: bool
) -> None:
    # Kill Now, tool approvals and the review draft act directly, without a turn: one
    # gate at the interactions dispatch (and on the draft-edit submit) refuses them in
    # an unbound code channel of ours, as the typed message is refused there.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import interactions, interactions_approvals
    from kiro_crew.slack.code_channels import recognition, routing
    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_set

    async def classify(*_a: Any) -> Any:
        return getattr(recognition.ChannelKind, kind), None

    stop_turn = AsyncMock(return_value="hard")
    slack = SimpleNamespace(post_message=AsyncMock(), post_ephemeral=AsyncMock())
    orch = SimpleNamespace(
        slack=slack,
        sessions=SimpleNamespace(stop_turn=stop_turn, get_session_for_thread=lambda ts: None),
    )
    kill = {
        "type": "block_actions",
        "user": {"id": SHARED},
        "channel": {"id": "C0UNBOUND"},
        "message": {"ts": "m1", "thread_ts": "t1"},
        "actions": [{"action_id": "stop_kill_now", "value": "slack:t1"}],
    }
    key = "C0UNBOUND|t1|d1"
    _review_drafts_set(key, "draft", SHARED)
    edit = {
        "user": {"id": SHARED},
        "view": {
            "private_metadata": key,
            "state": {
                "values": {"mc_review_edit_block": {"mc_review_edit_input": {"value": "edited"}}}
            },
        },
    }

    async def permitted(_c: str) -> bool:
        return True

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(routing.recognition, "classify_channel", classify),
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "sel", lambda: MagicMock()),
        patch.object(interactions, "channel_inbound_permitted", permitted),
        patch.object(interactions, "channel_refusal", lambda *a, **k: False),
        patch.object(interactions, "is_shared_user", lambda u: u == SHARED),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
        patch.object(interactions, "prime_shared_user", AsyncMock()),
    ):
        asyncio.run(interactions.dispatch(kill))
        asyncio.run(interactions_approvals._handle_review_edit_submit(edit))
        if refused:
            # A refused revision keeps the draft (it is not popped before the gate).
            _review_drafts_set(key, "draft", SHARED)
            revise = {
                "user": {"id": SHARED},
                "view": {
                    "private_metadata": key,
                    "state": {
                        "values": {
                            "mc_review_revise_block": {"mc_review_revise_input": {"value": "x"}}
                        }
                    },
                },
            }
            asyncio.run(interactions_approvals._handle_review_revise_submit(revise))
            assert _review_drafts_get(key)[0] == "draft"
    assert stop_turn.await_count == (0 if refused else 1)
    posted = [c.args[1] for c in slack.post_message.await_args_list]
    assert ("edited" in posted) is not refused


def test_a_stale_card_in_an_unbound_code_channel_of_ours_is_not_edited(env: Any) -> None:
    # The "Choose several" submission skips the button gate; a superseded card would
    # be claimed and edited by the stale cleanup. The destination refusal runs first.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import interactions, interactions_options
    from kiro_crew.slack.code_channels import recognition, routing

    async def classify(*_a: Any) -> Any:
        return recognition.ChannelKind.OURS, None

    async def stale(*_a: Any) -> bool:
        return True

    refused_stale = AsyncMock()
    blocks = [
        {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": "options_choice_0", "value": "yes"},
                {"type": "button", "action_id": "options_choice_1", "value": "no"},
            ],
        }
    ]
    orch = SimpleNamespace(slack=env.slack, dashboard_state=None)
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(routing.recognition, "classify_channel", classify),
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
        patch.object(interactions, "_tell_user", AsyncMock()),
        patch.object(interactions, "sel", lambda: MagicMock()),
        patch.object(interactions, "options_control_is_stale", stale),
        patch.object(interactions, "_refuse_stale_options", refused_stale),
    ):
        asyncio.run(
            interactions_options._answer_options(
                channel="C0UNBOUND",
                msg_ts="m1",
                thread_ts="555.000001",
                user_id=SHARED,
                team_id="T1",
                block_id=None,
                blocks=blocks,
                selected_indices=[0],
                tool_name="options",
            )
        )
    refused_stale.assert_not_awaited()


def test_a_revision_into_a_code_channel_session_keeps_the_draft(env: Any) -> None:
    # The thread was linked to a code channel's session after the modal opened: the
    # revision would run there, so it is refused before the draft is consumed.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import handler, interactions, interactions_approvals
    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_set

    _bind(env)
    sessions = SimpleNamespace(get_session_for_thread=lambda ts: f"dashboard:{SLOT}")
    state = SimpleNamespace(get_linked_slot=lambda ts: None, sessions=sessions)
    slack = SimpleNamespace(post_message=AsyncMock(), post_ephemeral=AsyncMock())
    orch = SimpleNamespace(slack=slack, sessions=sessions)
    key = "D0DM00001|555.000001|d2"
    _review_drafts_set(key, "draft", SHARED)
    revise = {
        "user": {"id": SHARED},
        "view": {
            "private_metadata": key,
            "state": {
                "values": {"mc_review_revise_block": {"mc_review_revise_input": {"value": "x"}}}
            },
        },
    }

    async def permitted(_c: str) -> bool:
        return True

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(handler, "_dashboard_state", state),
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "sel", lambda: MagicMock()),
        patch.object(interactions, "channel_inbound_permitted", permitted),
        patch.object(interactions, "channel_refusal", lambda *a, **k: False),
        patch.object(interactions, "is_shared_user", lambda u: u == SHARED),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
    ):
        asyncio.run(interactions_approvals._handle_review_revise_submit(revise))
    assert _review_drafts_get(key)[0] == "draft"


def test_an_overlapping_draft_edit_is_posted_once(env: Any) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import interactions, interactions_approvals
    from kiro_crew.slack.handler import _review_drafts_set

    slack = SimpleNamespace(post_message=AsyncMock(), post_ephemeral=AsyncMock())
    orch = SimpleNamespace(slack=slack)
    key = "D0DM00001|555.000001|d3"
    _review_drafts_set(key, "draft", SHARED)
    edit = {
        "user": {"id": SHARED},
        "view": {
            "private_metadata": key,
            "state": {
                "values": {"mc_review_edit_block": {"mc_review_edit_input": {"value": "edited"}}}
            },
        },
    }

    async def permitted(_c: str) -> bool:
        return True

    async def both() -> None:
        await asyncio.gather(
            interactions_approvals._handle_review_edit_submit(edit),
            interactions_approvals._handle_review_edit_submit(edit),
        )

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "sel", lambda: MagicMock()),
        patch.object(interactions, "channel_inbound_permitted", permitted),
        patch.object(interactions, "channel_refusal", lambda *a, **k: False),
        patch.object(interactions, "is_shared_user", lambda u: u == SHARED),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
        patch.object(interactions_approvals, "_delete_review_placeholder", AsyncMock()),
    ):
        asyncio.run(both())
    posted = [c.args[1] for c in slack.post_message.await_args_list]
    assert posted.count("edited") == 1


def test_a_bang_stop_relinked_to_a_code_channel_session_while_checked_stops_nothing(
    env: Any,
) -> None:
    # dm_single_session: the Stop resolves the thread's linked owner. The owner relinks
    # the thread to a code channel's session while the shared user's Stop is checked.
    from unittest.mock import AsyncMock, MagicMock

    from slack_events_coverage_helpers import _event, _make_orch

    from kiro_crew.config.loader import SlackConfig

    _bind(env)
    orch = _make_orch()
    orch._cfg.slack = SlackConfig(dm_single_session=True)
    relinked: list[bool] = []
    orch.sessions.get_session_for_thread = MagicMock(
        side_effect=lambda ts: f"dashboard:{SLOT}" if relinked else "dashboard:chat-other"
    )
    real_refusal = events.shared_user_refused_for_session

    async def refusal(*args: Any) -> bool:
        refused = await real_refusal(*args)
        relinked.append(True)  # the owner relinks while this check awaits
        return refused

    orch.sessions.has_session = MagicMock(return_value=True)
    orch.sessions.note_stop = MagicMock(return_value=True)
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    a, b = _as_shared()
    with (
        a,
        b,
        patch("kiro_crew.slack.events.sel", return_value=MagicMock()),
        patch("kiro_crew.slack.events.is_owner", return_value=False),
        patch("kiro_crew.slack.events.is_allowed_user", return_value=False),
        patch("kiro_crew.slack.events.is_shared_user", lambda u: u == SHARED),
        patch("kiro_crew.slack.events.is_shared_listed", lambda u: u == SHARED),
        patch.object(events, "shared_user_refused_for_session", refusal),
    ):
        asyncio.run(
            events._route_message(
                orch,
                _event(text="!stop", user=SHARED, channel="D0DM00001", thread_ts="90.0"),
                events.SeenCache(),
            )
        )
    orch.sessions.stop_turn.assert_not_awaited()
    orch.sessions.note_stop.assert_not_called()


@pytest.mark.parametrize("text", ["!compact", "!COMPACT"])
@pytest.mark.parametrize("how", ["owner", "relinked"])
def test_a_shared_users_bang_command_never_acts_on_a_code_channel_session(
    env: Any, how: str, text: str
) -> None:
    # !compact acts on the session it was handed: refused when the thread's owner is a
    # code channel's session, or when the owner relinks it there while it is judged.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.slack import handler

    _bind(env)
    relinked: list[bool] = []
    code_session = f"dashboard:{SLOT}"

    def owner(_ts: str) -> str:
        if how == "owner" or relinked:
            return code_session
        return "dashboard:chat-other"

    sessions = SimpleNamespace(get_session_for_thread=owner)
    real = handler.shared_user_refused_for_session

    async def refusal(*args: Any) -> bool:
        refused = await real(*args)
        relinked.append(True)
        return refused

    compact = AsyncMock()
    slack = SimpleNamespace(post_message=AsyncMock())
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(handler, "shared_user_refused_for_session", refusal),
        patch.object(handler, "_handle_compact_command", compact),
        patch.object(handler, "is_shared_user", lambda u: u == SHARED),
    ):
        handled = asyncio.run(
            handler._route_bang_command(
                text, slack, sessions, "D0DM00001", "555.0", "m1", "slack:555.0", SHARED, None
            )
        )
    assert handled is True
    compact.assert_not_awaited()


def test_a_fallback_stop_relinked_to_a_code_channel_session_records_no_stop(env: Any) -> None:
    # "!stop now" reaches _bang_stop after the router's check and an await (the
    # deprecation notice): the session it records the Stop on is judged again there.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import handler

    _bind(env)
    sessions = SimpleNamespace(
        get_session_for_thread=lambda ts: f"dashboard:{SLOT}",
        has_session=lambda k: True,
        is_compacting=lambda k: False,
    )
    noted = MagicMock()
    slack = SimpleNamespace(post_message=AsyncMock(), post_ephemeral=AsyncMock())
    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(handler, "note_user_stop", noted),
        patch.object(handler, "compaction_in_flight", lambda *_a: False),
    ):
        asyncio.run(
            handler._bang_stop(
                "!stop now",
                slack,
                sessions,
                "D0DM00001",
                "555.0",
                "m1",
                "slack:555.0",
                SHARED,
                None,
            )
        )
    noted.assert_not_called()


def test_a_revision_relinked_while_judged_keeps_the_draft(env: Any) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.slack import handler, interactions, interactions_approvals
    from kiro_crew.slack.handler import _review_drafts_get, _review_drafts_set

    _bind(env)
    relinked: list[bool] = []
    sessions = SimpleNamespace(
        get_session_for_thread=lambda ts: f"dashboard:{SLOT}" if relinked else "dashboard:c-2"
    )
    state = SimpleNamespace(get_linked_slot=lambda ts: None, sessions=sessions)
    real = interactions_approvals.shared_user_refused_for_session

    async def refusal(*args: Any) -> bool:
        refused = await real(*args)
        relinked.append(True)
        return refused

    slack = SimpleNamespace(post_message=AsyncMock(), post_ephemeral=AsyncMock())
    orch = SimpleNamespace(slack=slack, sessions=sessions)
    key = "D0DM00001|555.000001|d4"
    _review_drafts_set(key, "draft", SHARED)
    revise = {
        "user": {"id": SHARED},
        "view": {
            "private_metadata": key,
            "state": {
                "values": {"mc_review_revise_block": {"mc_review_revise_input": {"value": "x"}}}
            },
        },
    }

    async def permitted(_c: str) -> bool:
        return True

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(handler, "_dashboard_state", state),
        patch.object(interactions, "_orch", orch),
        patch.object(interactions, "sel", lambda: MagicMock()),
        patch.object(interactions, "channel_inbound_permitted", permitted),
        patch.object(interactions, "channel_refusal", lambda *a, **k: False),
        patch.object(interactions, "is_shared_user", lambda u: u == SHARED),
        patch.object(interactions, "is_shared_listed", lambda u: u == SHARED),
        patch.object(interactions, "shared_user_refused_for_session", refusal),
    ):
        asyncio.run(interactions_approvals._handle_review_revise_submit(revise))
    assert _review_drafts_get(key)[0] == "draft"


def test_a_tab_running_a_code_channels_session_under_another_name_is_refused(env: Any) -> None:
    # A workflow tab kept on its origin's session (linked_session_key) is a code
    # channel's session whatever the tab is called; the thread linked to it is too.
    from types import SimpleNamespace

    from kiro_crew.slack import handler

    _bind(env)
    tab = SimpleNamespace(key="chat-wf-1", linked_session_key=f"dashboard:{SLOT}")
    other = SimpleNamespace(key="chat-plain", linked_session_key="")
    state = SimpleNamespace(
        _slots={"chat-wf-1": tab, "chat-plain": other},
        get_linked_slot=lambda ts: tab,
        sessions=None,
    )
    from kiro_crew.slack.code_channels import routing

    a, b = _as_shared()
    with (
        a,
        b,
        patch.object(handler, "_dashboard_state", state),
        patch.object(routing, "_dashboard_state", lambda: state),
    ):
        assert shared_user_refused_for_slot("chat-wf-1", "") is True
        assert shared_user_refused_for_slot("dashboard:chat-wf-1", "") is True
        assert shared_user_refused_for_slot("chat-plain", "") is False
        outcome = asyncio.run(
            route_linked_thread(
                "hi", "slack:555.000001", SHARED, "D0DM00001", env.slack, "555.000001"
            )
        )
    assert outcome is LinkedRoute.REFUSED
