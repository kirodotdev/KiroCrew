"""A code channel's agent and approval mode change only the session its binding recorded.

The binding store is the gateway's own record; a slot's ``linked_session_key``
is read back from its transcript, which an agent can rewrite. A code channel's
session is never linked to another, so a link read back onto it names some
other dashboard session, and every owner control refuses rather than change
that session: at entry, and again at its commit when the link lands while the
change awaits.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_code_channel_surface_helpers import CH, SLOT, env_fixture  # noqa: F401
from slack_code_channels_helpers import OWNER
from test_slack_code_channel_approval_select import _mode_of, _run_slash
from test_slack_code_channel_policy_controls import _menu
from test_slack_code_channel_trust_from_slack import _slot, chan  # noqa: F401

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.slack.code_channels import agent_picker, approval_picker
from kiro_crew.slack.code_channels.binding import channel_session
from kiro_crew.slack.code_channels.store import store


def _victim(chan: Any) -> Any:  # noqa: F811
    """An unrelated dashboard session the forged link names."""
    other = _slot(key="chat-9-9", channel="")
    chan.state._slots[other.key] = other
    return other


def _forge(chan: Any, other: Any) -> None:  # noqa: F811
    chan.slot.linked_session_key = effective_session_key(other)


def test_channel_session_is_the_recorded_unlinked_session(chan: Any) -> None:  # noqa: F811
    binding = store().get_binding(CH)
    assert binding is not None and channel_session(chan.state, binding) is chan.slot
    _forge(chan, _victim(chan))
    assert channel_session(chan.state, binding) is None


def test_a_slot_under_another_key_is_not_the_channels_session(chan: Any) -> None:  # noqa: F811
    binding = store().get_binding(CH)
    assert binding is not None
    chan.slot.key = "chat-7-7"  # the link index points at a slot the binding never named
    assert channel_session(chan.state, binding) is None


@pytest.mark.parametrize("text", ["approval trust", "approval reads", "approval normal"])
def test_a_forged_link_gets_no_mode_change_from_the_command(
    chan: Any, text: str  # noqa: F811
) -> None:
    other = _victim(chan)
    other._trust = text == "approval normal"
    _forge(chan, other)
    _run_slash(chan, text)
    assert chan.mode_rows == []
    assert _mode_of(other) == ("trust" if text == "approval normal" else "normal")


def test_a_forged_link_gets_no_trust_from_the_kickoff_select(chan: Any) -> None:  # noqa: F811
    other = _victim(chan)
    _forge(chan, other)
    assert asyncio.run(approval_picker.handle_select(chan.orch, _menu("trust")))
    assert chan.mode_rows == [] and _mode_of(other) == "normal"


def test_a_link_forged_while_trust_awaits_is_refused_at_the_grant(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Past every entry check: only the grant fence can refuse."""
    other = _victim(chan)
    real = approval_picker._apply

    async def forged_mid_change(state: Any, slot: Any, mode: str, user: str, *a: Any, **k: Any):
        _forge(chan, other)
        return await real(state, slot, mode, user, *a, **k)

    monkeypatch.setattr(approval_picker, "_apply", forged_mid_change)
    binding = store().get_binding(CH)
    assert binding is not None
    out = asyncio.run(
        approval_picker.set_channel_mode(binding, "trust", OWNER, via="command", team="T1")
    )
    assert out.result != "set"
    assert _mode_of(other) == "normal" and _mode_of(chan.slot) == "normal"


def _no_window_load(monkeypatch: pytest.MonkeyPatch) -> None:
    async def loaded(_slot: Any) -> bool:
        return True

    monkeypatch.setattr(agent_picker, "_load_window", loaded)


def test_a_forged_link_gets_no_agent_switch(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _no_window_load(monkeypatch)
    calls: list[str] = []

    async def switch(state: Any, slot: Any, agent: str, still_permitted: Any):
        calls.append(agent)
        return True, {}

    monkeypatch.setattr(agent_picker, "_switch", switch)
    _forge(chan, _victim(chan))
    binding = store().get_binding(CH)
    assert binding is not None
    out = asyncio.run(
        agent_picker.switch_channel_agent(
            binding, "builder", OWNER, via="command", team="T1", orch=chan.orch
        )
    )
    assert out.result == "unavailable" and calls == []


def test_a_link_forged_during_the_switch_is_refused_at_its_commit(
    chan: Any, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    _no_window_load(monkeypatch)
    other = _victim(chan)
    seen: list[bool] = []

    async def forged_mid_switch(state: Any, slot: Any, agent: str, still_permitted: Any):
        assert still_permitted()  # the gates admit it before the forgery
        _forge(chan, other)
        seen.append(still_permitted())
        return False, {"code": agent_picker.CALLER_NOT_PERMITTED}

    monkeypatch.setattr(agent_picker, "_switch", forged_mid_switch)
    binding = store().get_binding(CH)
    assert binding is not None
    asyncio.run(
        agent_picker.switch_channel_agent(
            binding, "builder", OWNER, via="command", team="T1", orch=chan.orch
        )
    )
    assert seen == [False]
    assert SLOT == chan.slot.key
