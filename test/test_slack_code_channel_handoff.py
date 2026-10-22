"""The code channel's hand-off notice and its owner-only Turn-on button.

When a code channel's own session is refused dispatch because
``slack.code_channel_dispatch`` is off, the refusal names the Settings control (not
the raw key), the channel gets ONE notice with a "Turn on hand-off for code
channels" button, and the owner's click on THAT notice writes the switch through
the Settings card's audited save. Nothing re-runs the refused tool call.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.code_channels import handoff, routing
from kiro_crew.slack.code_channels import surface as cc_surface

OWNER = "U0OWNER01"
OTHER = "U0OTHER01"
CHANNEL = "C0CODECHAN1"
TEAM = "T0TEAM001"
REPO = Path(__file__).resolve().parents[1]


class _Slack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, list[dict], str]] = []
        self.updates: list[tuple[str, str, str, list[dict] | None]] = []
        self.ephemerals: list[tuple[str, str, str]] = []

    async def post_blocks(self, channel: str, blocks: list[dict], text: str, thread_ts=None) -> str:
        self.posts.append((channel, blocks, text))
        return f"1700000000.00000{len(self.posts)}"

    async def update_message(self, channel: str, ts: str, text: str = "", blocks=None) -> None:
        self.updates.append((channel, ts, text, blocks))

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        self.ephemerals.append((channel, user, text))


class _Sel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A bound code channel, a fake Slack, a real config.json the save writes."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"slack": {"code_channels": True}}), encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(loader, "config_local_path", lambda: tmp_path / "config.local.json")
    sel = _Sel()
    monkeypatch.setattr(messaging, "_sel", lambda: sel)
    monkeypatch.setattr(messaging, "_hot_apply_after_write", AsyncMock())
    slack = _Slack()
    audits: list[tuple] = []
    binding = SimpleNamespace(channel_id=CHANNEL, team_id=TEAM, archived_at=0.0)
    monkeypatch.setattr(cc_surface, "slack", lambda: slack)
    monkeypatch.setattr(cc_surface, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(cc_surface, "outbound_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_surface, "is_archived", AsyncMock(return_value=False))
    monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    monkeypatch.setattr(cc_surface, "dashboard_state", lambda: None)
    monkeypatch.setattr(
        handoff,
        "store",
        lambda: SimpleNamespace(get_binding=lambda c: binding if c == CHANNEL else None),
    )
    monkeypatch.setattr(handoff, "code_channels_on", lambda cfg: True)
    monkeypatch.setattr(handoff, "live_config", lambda orch=None: object())
    monkeypatch.setattr(routing, "_origin_of", lambda channel, binding, x: "C0ORIGIN01")
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, channel, origin: True)
    monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    handoff.clear()
    yield SimpleNamespace(cfg=cfg, sel=sel, slack=slack, audits=audits)
    handoff.clear()


def _click(user: str, *, value: str = CHANNEL, ts: str = "1700000000.000001") -> dict[str, Any]:
    return {
        "team": {"id": TEAM},
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {"ts": ts},
        "actions": [{"action_id": handoff.HANDOFF_ACTION_ID, "value": value}],
    }


def _dispatch_on(cfg: Path) -> bool:
    return json.loads(cfg.read_text(encoding="utf-8"))["slack"].get("code_channel_dispatch") is True


async def _drain() -> None:
    """Wait for every notice post the judgement started."""
    me = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not me]
    if pending:
        await asyncio.gather(*pending)


def _issue(world) -> str:
    """Post the current notice the way a refusal does; return its ts."""

    async def body() -> None:
        assert handoff.notice_soon(CHANNEL) is True
        await _drain()

    asyncio.run(body())
    assert world.slack.posts, "the notice was posted"
    return f"1700000000.00000{len(world.slack.posts)}"


# ── the refusal names the control, not the key ─────────────────────────────


def test_the_refusal_names_the_settings_control_and_the_key_only_in_parentheses(monkeypatch):
    monkeypatch.setattr(
        cce,
        "_live_config",
        lambda: SimpleNamespace(slack=SimpleNamespace(code_channel_dispatch=False)),
    )
    monkeypatch.setattr("kiro_crew.slack.code_channels.binding.code_channels_on", lambda cfg: True)
    why = cce._switch_refusal()
    assert why == cce.DISPATCH_OFF_REFUSAL
    catalog = json.loads((REPO / "website/src/i18n/locales/en.manual.json").read_text("utf-8"))

    def find(node: Any) -> str | None:
        if isinstance(node, dict):
            if "slackCode" in node and isinstance(node["slackCode"], dict):
                return node["slackCode"].get("code_channel_dispatch")
            for v in node.values():
                if (hit := find(v)) is not None:
                    return hit
        return None

    label = find(catalog)
    assert label == "Let the conductor hand off work"
    assert f'"{label}"' in why
    assert "Settings → Channels → Slack → Code channels" in why
    assert "(slack.code_channel_dispatch)" in why
    assert why.count("slack.code_channel_dispatch") == 1
    assert handoff.BUTTON_TEXT in why


# ── one notice per channel per refusal state ───────────────────────────────


def test_the_notice_is_posted_once_per_switch_off(world, monkeypatch):
    state = {"switch": cce.DISPATCH_OFF_REFUSAL}
    monkeypatch.setattr(cce, "_switch_refusal", lambda: state["switch"])

    async def body() -> None:
        for _ in range(3):  # three refused hand-offs in a row
            cce._note_handoff_state(CHANNEL)
            await _drain()
        assert len(world.slack.posts) == 1
        state["switch"] = ""  # turned on (Settings card or the button)
        cce._note_handoff_state(CHANNEL)
        state["switch"] = "the config files have not been checked yet"  # neither on nor off
        cce._note_handoff_state(CHANNEL)
        await _drain()
        assert len(world.slack.posts) == 1
        state["switch"] = cce.DISPATCH_OFF_REFUSAL  # turned off again: a fresh notice
        cce._note_handoff_state(CHANNEL)
        cce._note_handoff_state(CHANNEL)
        await _drain()

    asyncio.run(body())
    assert len(world.slack.posts) == 2
    channel, blocks, _ = world.slack.posts[0]
    assert channel == CHANNEL
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == handoff.HANDOFF_ACTION_ID
    assert button["text"]["text"] == "Turn on hand-off for code channels"
    assert "confirm" in button, "one confirm before the write"


def test_a_refused_post_lets_the_next_refusal_try_again(world, monkeypatch):
    monkeypatch.setattr(world.slack, "post_blocks", AsyncMock(return_value=""))

    async def body() -> None:
        assert handoff.notice_soon(CHANNEL) is True
        await _drain()
        assert handoff.notice_soon(CHANNEL) is True
        await _drain()

    asyncio.run(body())


def test_a_lost_answer_keeps_the_channel_noticed(world, monkeypatch):
    """Slack may have posted it: no second notice in the same switch-off."""
    lost = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(world.slack, "post_blocks", lost)

    async def body() -> None:
        assert handoff.notice_soon(CHANNEL) is True
        await _drain()
        assert handoff.notice_soon(CHANNEL) is False

    asyncio.run(body())
    assert lost.await_count == 1


@pytest.mark.parametrize("gate", ["workspace", "channel", "governance", "binding"])
def test_no_notice_where_the_channel_may_not_be_posted_to(world, monkeypatch, gate):
    """Refused before posting: no notice, and the channel is not marked noticed,
    so once the gate clears the next refusal posts it."""
    shut = {"on": True}
    if gate == "workspace":
        monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: not shut["on"])
    elif gate == "channel":
        monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, c, o: not shut["on"])
    elif gate == "governance":

        async def _permitted() -> bool:
            return not shut["on"]

        monkeypatch.setattr(cc_surface, "outbound_permitted", _permitted)
    else:

        async def _archived(binding) -> bool:
            return shut["on"]

        monkeypatch.setattr(cc_surface, "is_archived", _archived)

    async def body() -> None:
        handoff.notice_soon(CHANNEL)
        await _drain()
        assert world.slack.posts == []
        shut["on"] = False
        assert handoff.notice_soon(CHANNEL) is True
        await _drain()

    asyncio.run(body())
    assert len(world.slack.posts) == 1


# ── the button ─────────────────────────────────────────────────────────────


def test_the_owners_click_turns_hand_off_on_through_the_audited_save(world):
    ts = _issue(world)
    assert asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts))) is True
    assert _dispatch_on(world.cfg)
    record = [c for c in world.sel.calls if c["operation"] == "slack.config.update"]
    assert record and record[-1]["outcome"] == "ok"
    assert record[-1]["resources"] == "code_channel_dispatch"
    assert record[-1]["caller"] == OWNER and record[-1]["source"] == "slack"
    assert any(a[1] == handoff.OPERATION and a[2] == "allowed" for a in world.audits)
    channel, upd_ts, text, blocks = world.slack.updates[-1]
    assert (channel, upd_ts, text) == (CHANNEL, ts, handoff.DONE_TEXT)
    assert handoff.DONE_TEXT == "Hand-off is on. Send your request again."
    assert handoff.DONE_TEXT in blocks[0]["text"]["text"]
    assert not any(b.get("type") == "actions" for b in blocks), "the button is gone"
    messaging._hot_apply_after_write.assert_awaited()


def test_a_non_owner_is_refused_and_nothing_is_written(world):
    ts = _issue(world)
    assert asyncio.run(handoff.handle_handoff_click(None, _click(OTHER, ts=ts))) is True
    assert not _dispatch_on(world.cfg)
    assert world.slack.ephemerals == [
        (CHANNEL, OTHER, handoff.NOT_OWNER_TEXT.format(owner=cc_surface.owner_name()))
    ]
    assert world.slack.updates == []
    assert not [c for c in world.sel.calls if c.get("outcome") == "ok"]
    assert any(a[1] == handoff.OPERATION and a[2] == "denied" for a in world.audits)


def test_a_button_the_gateway_did_not_post_is_not_consent(world):
    """An agent-posted copy of the button (any label, no confirm) is refused."""
    _issue(world)
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts="1700000099.000001")))
    assert not _dispatch_on(world.cfg)
    assert world.slack.ephemerals[-1] == (CHANNEL, OWNER, handoff.STALE_TEXT)
    assert any(a[2] == "denied" and "not the current" in a[4] for a in world.audits)


def test_a_click_with_no_notice_issued_is_refused(world):
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER)))
    assert not _dispatch_on(world.cfg)
    assert world.slack.ephemerals[-1] == (CHANNEL, OWNER, handoff.STALE_TEXT)


def test_a_replayed_click_does_not_turn_it_on_again(world):
    ts = _issue(world)
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert _dispatch_on(world.cfg)
    world.cfg.write_text(json.dumps({"slack": {"code_channels": True}}), encoding="utf-8")
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert not _dispatch_on(world.cfg), "single use"
    assert world.slack.ephemerals[-1] == (CHANNEL, OWNER, handoff.STALE_TEXT)


def test_a_button_for_another_channel_is_refused(world):
    ts = _issue(world)
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, value="C0SOMEWHERE", ts=ts)))
    assert not _dispatch_on(world.cfg)
    assert world.slack.ephemerals == [(CHANNEL, OWNER, handoff.NOT_ALLOWED_TEXT)]


@pytest.mark.parametrize("gate", ["workspace", "channel", "governance"])
def test_a_click_the_channel_gates_refuse_changes_nothing(world, monkeypatch, gate):
    ts = _issue(world)
    if gate == "workspace":
        monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: False)
    elif gate == "channel":
        monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, c, o: False)
    else:
        monkeypatch.setattr(cc_surface, "outbound_permitted", AsyncMock(return_value=False))
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert not _dispatch_on(world.cfg)
    assert world.slack.updates == []
    assert any(a[1] == handoff.OPERATION and a[2] == "denied" for a in world.audits)


def test_an_overlay_pin_refuses_the_click_as_the_card_does(world, tmp_path):
    ts = _issue(world)
    (tmp_path / "config.local.json").write_text(
        json.dumps({"slack": {"code_channel_dispatch": False}}), encoding="utf-8"
    )
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert not _dispatch_on(world.cfg)
    # Said once, on the notice itself: no private copy of the same refusal.
    assert world.slack.ephemerals == []
    denied = [c for c in world.sel.calls if c.get("outcome") == "denied"]
    assert denied and denied[-1]["operation"] == "slack.config.update"
    # The button can only fail again while the file pins it: it is swapped for
    # where the setting lives, and a replayed click is refused as stale.
    _, updated_ts, _, blocks = world.slack.updates[-1]
    assert updated_ts == ts and not any(b["type"] == "actions" for b in blocks)
    pinned = handoff.PINNED_TEXT.format(path=str(tmp_path / "config.local.json"))
    assert blocks[-1]["elements"][0]["text"] == pinned
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert world.slack.ephemerals[-1][2] == handoff.STALE_TEXT
    assert not _dispatch_on(world.cfg)


def test_the_click_is_not_ours_while_code_channels_are_off(world, monkeypatch):
    monkeypatch.setattr(handoff, "code_channels_on", lambda cfg: False)
    assert asyncio.run(handoff.handle_handoff_click(None, _click(OWNER))) is False
    assert not _dispatch_on(world.cfg)


def test_the_click_never_retries_the_refused_call(world, monkeypatch):
    """Only the setting and the notice change; no session is driven."""
    import kiro_crew.dashboard.session_control as sc

    calls: list[Any] = []
    for verb in ("create_session", "send_to_session"):
        if hasattr(sc, verb):
            monkeypatch.setattr(sc, verb, lambda *a, **k: calls.append(a))
    ts = _issue(world)
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert _dispatch_on(world.cfg) and calls == []


def test_the_interaction_router_reaches_the_owner_check(world, monkeypatch):
    from kiro_crew.slack import interactions

    monkeypatch.setattr(interactions, "is_allowed_user", lambda user: True)
    monkeypatch.setattr(interactions, "channel_inbound_permitted", AsyncMock(return_value=True))
    ts = _issue(world)
    asyncio.run(interactions.dispatch({"type": "block_actions", **_click(OTHER, ts=ts)}))
    assert not _dispatch_on(world.cfg)
    assert world.slack.ephemerals[-1] == (
        CHANNEL,
        OTHER,
        handoff.NOT_OWNER_TEXT.format(owner=cc_surface.owner_name()),
    )
    asyncio.run(interactions.dispatch({"type": "block_actions", **_click(OWNER, ts=ts)}))
    assert _dispatch_on(world.cfg)


# ── agents may not wear the gateway's buttons ──────────────────────────────


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {
                "type": "actions",
                "elements": [{"type": "button", "action_id": "kc_code_channel_enable_handoff"}],
            }
        ],
        [{"type": "actions", "block_id": "KC_Code_Channel_x", "elements": []}],
        [
            {
                "type": "section",
                "accessory": {"type": "button", "action_id": "kc_code_channel_archive"},
            }
        ],
    ],
)
def test_agent_blocks_with_a_reserved_action_id_are_refused(blocks):
    from kiro_crew.dashboard.messaging_api.proactive_send import _reserved_action_refusal
    from kiro_crew.slack.client import BLOCKS_RESERVED_ACTION_ERROR

    response = _reserved_action_refusal(blocks)
    assert response is not None and response.status == 400
    assert json.loads(response.body)["code"] == BLOCKS_RESERVED_ACTION_ERROR


def test_ordinary_agent_buttons_are_left_alone():
    from kiro_crew.dashboard.messaging_api.proactive_send import _reserved_action_refusal

    blocks = [{"type": "actions", "elements": [{"type": "button", "action_id": "approve_1"}]}]
    assert _reserved_action_refusal(blocks) is None
    assert _reserved_action_refusal(None) is None


def test_a_superseded_attempt_neither_releases_nor_posts_for_the_newer_one(world, monkeypatch):
    """A (held in its governance check) -> forget -> B reserves -> A is denied:
    A must not release B's reservation, so a further refusal starts nothing and
    only B posts."""
    gate_a = asyncio.Event()
    answers = iter([False, True])  # A is denied, B is permitted

    async def _permitted() -> bool:
        answer = next(answers, True)
        if answer is False:
            await gate_a.wait()
        return answer

    monkeypatch.setattr(cc_surface, "outbound_permitted", _permitted)

    async def body() -> None:
        assert handoff.notice_soon(CHANNEL) is True  # A
        await asyncio.sleep(0)
        handoff.forget(CHANNEL)  # the switch was seen on, then off again
        assert handoff.notice_soon(CHANNEL) is True  # B
        gate_a.set()  # A comes back denied
        await _drain()
        assert handoff.notice_soon(CHANNEL) is False, "B's notice still stands"
        await _drain()

    asyncio.run(body())
    assert len(world.slack.posts) == 1


def test_a_failed_config_write_keeps_the_button_for_a_retry(world, monkeypatch):
    """Full disk / read-only storage: a refusal, the notice still stands."""
    ts = _issue(world)
    real = messaging._LockedSectionWrite.commit

    async def _full(self: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(messaging._LockedSectionWrite, "commit", _full)
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert not _dispatch_on(world.cfg)
    assert world.slack.ephemerals[-1][2] == handoff.FAILED_TEXT.format(
        reason="config.json could not be written"
    )
    denied = [c for c in world.sel.calls if c.get("outcome") == "denied"]
    assert denied and denied[-1]["error"] == "config.json could not be written"
    monkeypatch.setattr(messaging._LockedSectionWrite, "commit", real)
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert _dispatch_on(world.cfg), "the storage recovered: the same button works"


def test_an_unexpected_save_error_keeps_the_button(world, monkeypatch):
    ts = _issue(world)
    monkeypatch.setattr(
        messaging, "save_code_channel_dispatch", AsyncMock(side_effect=RuntimeError())
    )
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert world.slack.ephemerals[-1][2].startswith("I couldn't turn on hand-off:")
    assert handoff._noticed[CHANNEL] == ts, "still the current notice"


def test_a_pinned_refusal_falls_back_to_a_private_reply_when_the_notice_cannot_change(
    world, tmp_path, monkeypatch
):
    ts = _issue(world)
    (tmp_path / "config.local.json").write_text(
        json.dumps({"slack": {"code_channel_dispatch": False}}), encoding="utf-8"
    )
    monkeypatch.setattr(world.slack, "update_message", AsyncMock(side_effect=RuntimeError()))
    asyncio.run(handoff.handle_handoff_click(None, _click(OWNER, ts=ts)))
    assert world.slack.ephemerals[-1][2].startswith("Hand-off for code channels is set in")


def test_the_pinned_reply_names_the_setting_and_file_not_a_button_or_key():
    text = handoff.PINNED_TEXT.format(path="~/.kiro/crew/config.local.json")
    assert text.startswith("Hand-off for code channels is set in `~/.kiro/crew/config.local.json`")
    assert "button" not in text and "slack.code_channel_dispatch" not in text


def test_the_hand_off_refusal_names_the_owner(world, monkeypatch):
    monkeypatch.setattr(slack_handler, "_owner_id", OWNER)
    ts = _issue(world)
    asyncio.run(handoff.handle_handoff_click(None, _click(OTHER, ts=ts)))
    assert world.slack.ephemerals[-1][2] == (
        f":no_entry: Only the Kiro Crew owner (<@{OWNER}>) can turn on hand-off."
    )
