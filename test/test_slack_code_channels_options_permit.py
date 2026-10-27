"""An OPTIONS answer in a bound code channel obeys the channel's activation rule.

A choice or a multi-select Send clicked in a code channel runs a turn in its
bound session, exactly like a typed message, so it must pass the same rule the
inbound gate applies: the code channel and its origin conversation both still
admit the channel's turns. A control posted before either went to ``review`` or
``off`` is refused at the click -- no footer edit, no ``handle_message`` -- and
the refusal is SEL-audited. An admitted channel still dispatches.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_code_channels_helpers import Env, make_env

from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW
from kiro_crew.slack import interactions as ix
from kiro_crew.slack.code_channels import routing
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_CHECKBOXES_ACTION,
    OPTIONS_SUBMIT_ACTION,
)
from kiro_crew.testing.wait import await_until

CODE = "C0CODE"
ORIGIN = "C0ORIGIN"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch, env: Env) -> MagicMock:
    o = MagicMock()
    o.slack = MagicMock()
    o.slack.update_message = AsyncMock()
    o.slack.post_message = AsyncMock(return_value="label-ts")
    o.slack.post_blocks = AsyncMock(return_value="fallback-ts")
    o.slack.delete_message = AsyncMock()
    o.dashboard_state = None
    o._handler_tasks = set()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
    monkeypatch.setattr(ix, "forget_slack_options_for_thread", MagicMock())
    monkeypatch.setattr(ix, "slack_options_owner_keys_snapshot", lambda state, ts: ())
    return o


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    run = AsyncMock()
    monkeypatch.setattr(ix, "handle_message", run)
    return run


def _bind(env: Env) -> None:
    env.store.put_binding(
        Binding(
            channel_id=CODE,
            team_id="T1ABC",
            session_key="chat-9-1",
            anchor_ts="300.000001",
            origin_channel_id=ORIGIN,
            origin_ts="1700000000.000100",
        )
    )
    assert env.state is not None
    env.state.linked["300.000001"] = SimpleNamespace(key="chat-9-1", running=False)


def _click() -> tuple[dict, dict]:
    element = {
        "type": "button",
        "action_id": f"{OPTIONS_ACTION_PREFIX}0",
        "text": {"type": "plain_text", "text": "Ship it"},
        "value": "Ship it",
    }
    payload = {
        "user": {"id": "UOWNER"},
        "team": {"id": "T1ABC"},
        "message": {"ts": "m1", "blocks": [{"type": "actions", "elements": [element]}]},
    }
    return payload, dict(element)


def _submit() -> dict:
    return {
        "user": {"id": "UOWNER"},
        "team": {"id": "T1ABC"},
        "message": {
            "ts": "m1",
            "blocks": [
                {
                    "type": "actions",
                    "block_id": "blk1",
                    "elements": [
                        {
                            "type": "checkboxes",
                            "action_id": OPTIONS_CHECKBOXES_ACTION,
                            "options": [
                                {"text": {"type": "plain_text", "text": c}, "value": c}
                                for c in ("A", "B")
                            ],
                        },
                        {
                            "type": "button",
                            "action_id": OPTIONS_SUBMIT_ACTION,
                            "text": {"type": "plain_text", "text": "Send"},
                        },
                    ],
                }
            ],
        },
        "state": {
            "values": {"blk1": {OPTIONS_CHECKBOXES_ACTION: {"selected_options": [{"value": "A"}]}}}
        },
    }


async def _answer(how: str) -> None:
    if how == "choice":
        payload, action = _click()
        await ix._handle_options(payload, action, CODE, "m1")
    elif how == "action-button":
        payload, action = _click()
        action["value"] = 'action::{"task": "deploy"}'
        await ix._handle_options(payload, action, CODE, "m1")
    else:
        await ix._handle_options_submit(_submit(), CODE, "m1")


async def _join(orch: MagicMock) -> None:
    tasks = list(orch._handler_tasks)
    if tasks:
        await await_until(asyncio.gather(*tasks), what="the handler's background tasks")


_HOW = ["choice", "action-button", "selection"]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", _HOW)
@pytest.mark.parametrize(
    "where, activation",
    [
        (ORIGIN, ACTIVATION_REVIEW),
        (ORIGIN, ACTIVATION_OFF),
        (CODE, ACTIVATION_REVIEW),
        (CODE, ACTIVATION_OFF),
    ],
    ids=["origin-review", "origin-off", "channel-review", "channel-off"],
)
async def test_an_answer_the_channel_no_longer_admits_runs_no_turn(
    env: Env, orch: MagicMock, turns: AsyncMock, how: str, where: str, activation: str
) -> None:
    _bind(env)
    env.activation[where] = activation
    await _answer(how)
    await _join(orch)

    turns.assert_not_awaited()
    orch.slack.update_message.assert_not_awaited()  # the footer is left as it was
    orch.slack.post_message.assert_not_awaited()
    (denied,) = env.sel.ops("slack.code_channel.options")
    assert denied["outcome"] == "denied" and denied["caller"] == "UOWNER"
    assert f"channel={CODE}" in denied["resources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", _HOW)
async def test_an_answer_in_an_admitting_code_channel_still_dispatches(
    env: Env, orch: MagicMock, turns: AsyncMock, how: str
) -> None:
    _bind(env)
    await _answer(how)
    await _join(orch)

    turns.assert_awaited_once()
    assert turns.await_args.args[2] == CODE
    assert env.sel.ops("slack.code_channel.options") == []


@pytest.mark.asyncio
async def test_an_unbound_channel_is_not_judged_by_code_channel_rules(
    env: Env, orch: MagicMock, turns: AsyncMock
) -> None:
    env.activation[CODE] = ACTIVATION_OFF  # no binding: the ordinary path's rules apply
    await _answer("choice")
    await _join(orch)
    turns.assert_awaited_once()


def test_a_failed_binding_lookup_refuses(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = MagicMock()
    broken.get_binding.side_effect = OSError("store unreadable")
    monkeypatch.setattr(routing, "store", lambda: broken)
    assert routing.options_answer_permitted(CODE, "UOWNER") is False
    (denied,) = env.sel.ops("slack.code_channel.options")
    assert denied["error"] == "permit lookup failed"


def test_code_channels_off_admits_every_answer(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(env)
    env.activation[ORIGIN] = ACTIVATION_REVIEW
    monkeypatch.setattr(routing, "code_channels_on", lambda cfg: False)
    assert routing.options_answer_permitted(CODE, "UOWNER") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("how", _HOW)
async def test_an_answer_in_a_channel_unbound_by_revocation_runs_no_turn(
    env: Env, orch: MagicMock, turns: AsyncMock, how: str
) -> None:
    """Slack stopped listing us, so the gate unbound the channel; a control its
    session posted before that must not run a turn there, even with the origin
    now in review (the binding that carried the origin is gone)."""
    _bind(env)
    env.activation[ORIGIN] = ACTIVATION_REVIEW
    assert env.store.drop_binding(CODE) is True
    await _answer(how)
    await _join(orch)

    turns.assert_not_awaited()
    orch.slack.update_message.assert_not_awaited()
    (denied,) = env.sel.ops("slack.code_channel.options")
    assert denied["outcome"] == "denied" and "no longer bound" in denied["error"]


def test_a_revocation_survives_a_restart_and_a_rebind_clears_it(env: Env, tmp_path: Any) -> None:
    from kiro_crew.slack.code_channels.store import CodeChannelStore

    _bind(env)
    env.store.drop_binding(CODE)
    reloaded = CodeChannelStore(env.store.path)
    assert reloaded.was_revoked(CODE) and not reloaded.was_revoked("C0ELSE")
    _bind(env)
    assert not env.store.was_revoked(CODE)
    assert not CodeChannelStore(env.store.path).was_revoked(CODE)
