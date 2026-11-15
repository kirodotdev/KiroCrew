"""Answer an agent's ask_question card from Slack (RFC notification-bridge-actions, B4e).

Drives the real path end to end on a real ``DashboardState``: the agent-ask card
opens, its hand-off binds the card and publishes the note, the real bridge
dispatcher routes it to the real Slack sink (only the Slack client is faked), and
a press on a posted button goes through ``interactions.dispatch``. Pins that:

* a card with one single-select question gets one button per option, and the
  owner's press answers the card exactly as the dashboard would for that option;
* any other card (several questions, multi-select) gets no buttons, and every
  note links to the chat, where the full card (free text included) is answered;
* a stranger's, second, settled-elsewhere or cross-kind press changes nothing and
  is audited; with the preview off nothing is posted or answered;
* the token is shown in no text and agent-authored labels cannot render markup.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from real_dashboard_state import (  # noqa: F401
    _settle_real_dashboard_states,
    real_dashboard_state,
)

from kiro_crew.notifications import bridge_actions as actions_mod
from kiro_crew.notifications import bridge_approval_note as approval_mod
from kiro_crew.notifications import bridge_question_note as note_mod
from kiro_crew.notifications.bridge_actions import (
    SLACK_ACTION_PREFIX,
    ActionOutcome,
    BridgeActionRegistry,
)
from kiro_crew.slack import interactions

_OWNER = "U-OWNER"
_DM = "D-OWNER"
_TS = "2.0"
_ASK = "a" * 32
_SK = "dashboard:chat-1"
_ONE = [
    {
        "question": "Which branch should I ship?",
        "header": "Branch",
        "options": [
            {"label": "main", "description": ""},
            {"label": "release <!channel> *now*", "description": ""},
        ],
        "multiSelect": False,
    }
]
#: The card's whole window, on the fake clock; nothing sleeps.
_WINDOW = {"ttl_s": 1800.0}  # flake-ok: a deadline on the fake clock, nothing waits


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Sel:
    def __getattr__(self, name: str) -> Any:
        return lambda **kw: None


class _FakeSlack:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.blocks: list[tuple[list[dict[str, Any]], str]] = []
        self.post_ephemeral = AsyncMock()

    async def open_dm(self, user_id: str) -> str | None:
        return _DM if user_id == _OWNER else None

    async def post_message(self, channel: str, text: str) -> str:
        self.texts.append(text)
        return "1.0"

    async def post_blocks(self, channel: str, blocks: list[dict], text: str) -> str:
        self.blocks.append((blocks, text))
        return _TS


@pytest.fixture()
def preview(monkeypatch) -> dict[str, bool]:
    live = {"on": True}
    monkeypatch.setattr(approval_mod, "enabled", lambda cfg: live["on"])
    return live


@pytest.fixture()
def audits(monkeypatch) -> list[tuple[ActionOutcome, dict[str, Any]]]:
    seen: list[tuple[ActionOutcome, dict[str, Any]]] = []
    monkeypatch.setattr(
        "kiro_crew.notifications.bridge_action_audit.audit_action",
        lambda registry, token, outcome, **kw: seen.append((outcome, kw)),
    )
    return seen


@pytest.fixture()
def gateway(tmp_path, monkeypatch, preview):
    monkeypatch.setattr(actions_mod, "bridge_clock", _Clock())
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Sel())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = real_dashboard_state(tmp_path)
    state.owner_id = _OWNER
    state.slack_client = _FakeSlack()
    state.get_or_create_slot("chat-1")
    # One dashboard tab is open, so the card stays up.
    monkeypatch.setattr(state, "deliver_ws_owners", AsyncMock(return_value=1))
    state.notification_channel_settings.update("system.approval", deliver_to=["slack"])
    orch = SimpleNamespace(slack=state.slack_client, dashboard_state=state)
    monkeypatch.setattr("kiro_crew.slack.interactions._orch", orch)
    monkeypatch.setattr("kiro_crew.slack.interactions.is_allowed_user", lambda uid: True)
    monkeypatch.setattr("kiro_crew.slack.interactions.is_owner", lambda uid: uid == _OWNER)
    monkeypatch.setattr(
        "kiro_crew.slack.interactions.channel_inbound_permitted", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("kiro_crew.slack.interactions.channel_refusal", lambda *a, **kw: None)
    return state


async def _open(state: Any, questions: list[dict] = _ONE) -> asyncio.Future[Any]:
    """Open the card the way the agent does, and deliver its note to Slack."""
    assert await state.open_agent_ask(_ASK, "chat-1", _SK, questions) == 1
    future = state._question_futures[_ASK]
    task = note_mod.schedule_pending_question(
        state,
        ask_id=_ASK,
        slot_key="chat-1",
        session_key=_SK,
        questions=state._agent_asks[_ASK]["questions"],
        session_title="Release notes",
        **_WINDOW,
    )
    assert task is not None
    await task
    await state.notification_bridge.drain()
    return future


def _buttons(state: Any) -> list[dict[str, Any]]:
    [(blocks, _text)] = state.slack_client.blocks
    return blocks[-1]["elements"]


async def _press(button: dict[str, Any], user: str = _OWNER, action: str = "") -> None:
    await interactions.dispatch(
        {
            "type": "block_actions",
            "user": {"id": user},
            "channel": {"id": _DM},
            "message": {"ts": _TS},
            "actions": [{"action_id": action or button["action_id"], "value": button["value"]}],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_the_owners_press_answers_the_card_as_the_dashboard_would(gateway, audits, index):
    future = await _open(gateway)
    labels = [o["label"] for o in _ONE[0]["options"]]
    await _press(_buttons(gateway)[index])
    assert future.result() == {_ONE[0]["question"]: labels[index]}
    assert gateway.recent_question_resolutions()[_ASK] == "answered"
    assert _ASK not in gateway._pending_questions, "the card is retired on every screen"
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.ADMITTED and kw["choice"] == f"option_{index}"


@pytest.mark.asyncio
async def test_the_note_links_to_the_chat_and_shows_no_token(gateway):
    await _open(gateway)
    [(blocks, fallback)] = gateway.slack_client.blocks
    shown = [fallback, *gateway.slack_client.texts]
    shown += [b["text"]["text"] for b in blocks if b["type"] == "section"]
    token = _buttons(gateway)[0]["value"]
    nonce = token.rsplit("|", 1)[-1]
    assert all(nonce not in text for text in shown)
    assert any("/chat?sid=chat-1" in text for text in shown)
    assert any("Which branch should I ship?" in text for text in shown)


@pytest.mark.asyncio
async def test_an_agent_authored_label_cannot_render_markup(gateway):
    await _open(gateway)
    button = _buttons(gateway)[1]
    assert button["text"]["type"] == "plain_text", "Slack never parses plain_text"


@pytest.mark.asyncio
async def test_a_strangers_press_changes_nothing_and_is_audited(gateway, audits):
    future = await _open(gateway)
    await _press(_buttons(gateway)[0], user="U-OTHER")
    assert not future.done()
    [(outcome, kw)] = audits
    assert outcome is ActionOutcome.NOT_OWNER and kw["actor"] == "U-OTHER"
    gateway.slack_client.post_ephemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_press_is_a_no_op(gateway, audits):
    future = await _open(gateway)
    first, second = _buttons(gateway)
    await _press(first)
    await _press(second)
    assert future.result() == {_ONE[0]["question"]: "main"}
    assert [o for o, _ in audits] == [ActionOutcome.ADMITTED, ActionOutcome.ALREADY_ANSWERED]


@pytest.mark.asyncio
async def test_a_card_answered_in_the_dashboard_is_not_changed(gateway, audits):
    future = await _open(gateway)
    assert gateway.resolve_question(_ASK, {_ONE[0]["question"]: "main"})
    await _press(_buttons(gateway)[1])
    assert future.result() == {_ONE[0]["question"]: "main"}
    assert [o for o, _ in audits] == [ActionOutcome.ALREADY_ANSWERED]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "deny", "option_5", "trust"])
async def test_an_answer_the_card_does_not_offer_is_refused(gateway, audits, action):
    future = await _open(gateway)
    await _press(_buttons(gateway)[0], action=f"{SLACK_ACTION_PREFIX}{action}")
    assert not future.done()
    assert [o for o, _ in audits] == [ActionOutcome.INVALID_CHOICE]


@pytest.mark.asyncio
async def test_with_the_preview_off_nothing_is_posted_or_bound(gateway, preview):
    preview["on"] = False
    await _open(gateway)
    assert gateway.slack_client.blocks == [] and gateway.slack_client.texts == []
    assert gateway.bridge_actions._bindings == {}


@pytest.mark.asyncio
async def test_turning_the_preview_off_stops_a_press_on_a_posted_button(gateway, audits, preview):
    future = await _open(gateway)
    preview["on"] = False
    await _press(_buttons(gateway)[0])
    assert not future.done() and audits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "questions",
    [
        [{**_ONE[0], "multiSelect": True}],
        [_ONE[0], {**_ONE[0], "question": "And the tag?"}],
    ],
    ids=["multi-select", "two-questions"],
)
async def test_a_card_one_press_cannot_answer_gets_only_the_dashboard_link(gateway, questions):
    await _open(gateway, questions)
    assert gateway.slack_client.blocks == []
    assert gateway.bridge_actions._bindings == {}
    assert any("/chat?sid=chat-1" in text for text in gateway.slack_client.texts)


@pytest.mark.asyncio
async def test_options_that_would_read_the_same_get_no_buttons(gateway):
    # Two long labels cut to Slack's button limit read the same, yet send different
    # answers; the owner answers such a card in the dashboard.
    stem = "src/kiro_crew/connections/vendors/microsoft/graph/calendar/recurring_events/"
    lookalike = [
        {
            **_ONE[0],
            "options": [{"label": f"{stem}create_occurrence.py"}, {"label": f"{stem}delete.py"}],
        }
    ]
    await _open(gateway, lookalike)
    assert gateway.slack_client.blocks == []
    assert any("/chat?sid=chat-1" in text for text in gateway.slack_client.texts)


def test_only_a_card_with_one_single_select_question_is_answerable():
    assert note_mod.answerable_options(_ONE) == (
        "Which branch should I ship?",
        ["main", "release <!channel> *now*"],
    )
    assert note_mod.answerable_options([{**_ONE[0], "multiSelect": True}]) is None
    assert note_mod.answerable_options([_ONE[0], _ONE[0]]) is None
    assert note_mod.answerable_options([{**_ONE[0], "options": []}]) is None
    blank = [{**_ONE[0], "options": [{"label": "main"}, {"label": "   "}]}]
    assert note_mod.answerable_options(blank) is None, "an empty button is refused whole"
    assert note_mod.answerable_options("junk") is None


@pytest.mark.asyncio
async def test_the_open_route_hands_an_open_card_to_the_bridge(gateway, monkeypatch):
    from kiro_crew.dashboard.handlers import ask_question as route

    handed: list[dict[str, Any]] = []
    monkeypatch.setattr(note_mod, "schedule_pending_question", lambda s, **kw: handed.append(kw))
    monkeypatch.setattr(route, "_attested_session", AsyncMock(return_value=_SK))
    request = SimpleNamespace(
        app={"state": gateway},
        json=AsyncMock(return_value={"questions": _ONE, "ask_id": _ASK}),
    )
    response = await route.api_agent_ask_open(request)
    assert response.status == 200
    [kw] = handed
    assert (kw["ask_id"], kw["slot_key"], kw["session_key"]) == (_ASK, "chat-1", _SK)
    assert kw["ttl_s"] == gateway._AGENT_ASK_WINDOW_DEFAULT


@pytest.mark.asyncio
async def test_a_question_binding_must_settle_through_its_card():
    registry = BridgeActionRegistry()
    future = asyncio.get_running_loop().create_future()
    with pytest.raises(ValueError):
        registry.bind(_SK, _ASK, future, choices={"option_0": {}}, **_WINDOW, kind="question")
    with pytest.raises(ValueError):
        registry.bind(_SK, _ASK, future, choices={"option_0": {}}, **_WINDOW)
    with pytest.raises(ValueError):
        registry.bind(
            _SK,
            _ASK,
            future,
            choices={"approve": "approved"},
            **_WINDOW,
            kind="question",
            settle=lambda a: True,
        )
