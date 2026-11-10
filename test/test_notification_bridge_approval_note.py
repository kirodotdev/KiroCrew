"""The pending-approval note (RFC notification-bridge-actions, phase B4p).

Pins that the note is published only with the preview on, exactly once per open
approval, carrying the producer meta key; that it reaches the bridge and never the
dashboard bell; that the Slack sink reuses a card already in a linked Slack thread;
and that the hand-off runs as its own held task that never fails silently.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.config.sections_dashboard import FEATURE_PREVIEW_FLAGS
from kiro_crew.notifications import bridge_approval_note as note_mod
from kiro_crew.notifications.bridge import BridgeTransportUnavailable
from kiro_crew.notifications.bridge_slack import SlackBridgeSink


def _cfg(on: bool) -> Any:
    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews={note_mod.PREVIEW: on}))


class _State:
    """Stands in for DashboardState: records what reaches the bridge and the bell."""

    def __init__(self) -> None:
        self.notification_bridge = object()
        self.bridged: list[dict[str, Any]] = []
        self.bell: list[Any] = []

    def _bridge_note(self, note: dict[str, Any], publisher: str = "") -> None:
        self.bridged.append(note)

    def notify(self, *args: Any, **kwargs: Any) -> None:
        self.bell.append((args, kwargs))

    def _deliver_note(self, note: dict[str, Any]) -> None:
        self.bell.append(note)


def _publish(state: _State, *, on: bool = True, slack_card: bool = False):
    return note_mod.publish_pending_approval(
        state,
        _cfg(on),
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        request_id="7",
        tool_title="execute_bash",
        session_title="Release notes",
        slack_card=slack_card,
    )


def test_the_preview_is_a_known_flag_and_off_by_default():
    assert note_mod.PREVIEW in FEATURE_PREVIEW_FLAGS
    assert not note_mod.enabled(SimpleNamespace(dashboard=SimpleNamespace(feature_previews={})))
    assert not note_mod.enabled(SimpleNamespace())


def test_with_the_preview_off_no_note_is_published():
    state = _State()
    assert _publish(state, on=False) is None
    assert state.bridged == [] and state.bell == []


def test_with_the_preview_on_one_note_reaches_the_bridge_and_not_the_bell():
    state = _State()
    note = _publish(state)
    assert state.bridged == [note]
    assert state.bell == [], "the pending note must never ring the dashboard bell"
    assert note["channel"] == "system.approval"
    assert note[note_mod.PRODUCER_KEY] == note_mod.PRODUCER
    assert note[note_mod.REQUEST_KEY] == "7"
    assert (note["slot"], note["session_key"]) == ("chat-1", "dashboard:chat-1")
    assert note_mod.is_pending_approval_note(note)
    assert note_mod.SLACK_CARD_KEY not in note
    assert "execute_bash" in note["body"] and "Release notes" in note["body"]
    assert "actions" not in note, "the note carries no buttons yet"


def test_a_config_pending_review_note_is_not_a_tool_approval():
    assert not note_mod.is_pending_approval_note({"channel": "system.approval"})


def test_a_gateway_without_a_bridge_publishes_nothing():
    state = _State()
    state.notification_bridge = None
    assert _publish(state) is None
    assert state.bridged == []


def test_a_failing_bridge_never_reaches_the_prompt():
    state = _State()

    def _boom(note: dict[str, Any], publisher: str = "") -> None:
        raise RuntimeError("bridge down")

    state._bridge_note = _boom  # type: ignore[method-assign]
    assert _publish(state) is None


class _FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    async def open_dm(self, user_id: str) -> str | None:
        return "D-OWNER"

    async def post_message(self, channel: str, text: str) -> str:
        self.posts.append((channel, text))
        return "1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("slack_card", [True, False])
async def test_the_slack_sink_reuses_a_card_already_in_slack(slack_card):
    state = _State()
    note = _publish(state, slack_card=slack_card)
    slack = _FakeSlack()
    sink = SlackBridgeSink(SimpleNamespace(slack_client=slack, owner_id="U-OWNER"))
    if slack_card:
        # A skip, so the dispatcher audits it as skipped and keeps the token.
        with pytest.raises(BridgeTransportUnavailable):
            await sink.deliver(note)
        assert slack.posts == [], "the linked card is the one to answer; no second copy"
    else:
        await sink.deliver(note)
        assert len(slack.posts) == 1


@pytest.fixture()
def handed(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", classmethod(lambda cls: _cfg(True)))
    monkeypatch.setattr(
        note_mod, "publish_pending_approval", lambda state, cfg, **kw: calls.append(kw)
    )
    return calls


_FIELDS = {"slot_key": "chat-1", "session_key": "dashboard:chat-1", "request_id": "7"}


@pytest.mark.asyncio
async def test_the_hand_off_runs_as_its_own_held_task(handed):
    """Returned before it runs, held in the gateway set, released when done."""
    state = _State()
    state._background_tasks = set()  # type: ignore[attr-defined]
    fut = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(state, fut, **_FIELDS)
    assert task is not None and handed == []
    assert state._background_tasks == {task}  # type: ignore[attr-defined]
    await task
    assert handed == [_FIELDS]
    assert state._background_tasks == set()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_approval_answered_while_the_config_loads_sends_nothing(handed):
    fut = asyncio.get_running_loop().create_future()
    task = note_mod.schedule_pending_approval(_State(), fut, **_FIELDS)
    fut.set_result("approved")
    await task
    assert handed == []


@pytest.mark.asyncio
async def test_a_failing_hand_off_is_held_by_the_module_and_logged(monkeypatch, caplog):
    def _boom(state, cfg, **kw):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr("kiro_crew.config.KiroCrewConfig.load", classmethod(lambda cls: _cfg(True)))
    monkeypatch.setattr(note_mod, "publish_pending_approval", _boom)
    fut = asyncio.get_running_loop().create_future()
    with caplog.at_level("WARNING", logger=note_mod.__name__):
        task = note_mod.schedule_pending_approval(_State(), fut, **_FIELDS)
        assert task in note_mod._HANDOFF_TASKS
        with pytest.raises(RuntimeError):
            await task
        note_mod._log_failure(task)
    assert "pending-approval bridge hand-off failed" in caplog.text
