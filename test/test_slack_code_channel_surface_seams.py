"""The small seams of ``slack.code_channels.surface``: each fails closed or quiet.

Every helper here sits on a path where a failure must not change a decision
(an audit row, a slots push) or must refuse (a workspace or channel policy that
cannot be read). These pin that each of them does.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack.code_channels import surface, wrapup
from kiro_crew.slack.code_channels.store import Binding


def _binding(channel: str = "C1", session_key: str = "chat-1-1") -> Binding:
    return Binding(channel_id=channel, team_id="T1", session_key=session_key, anchor_ts="1.1")


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("boom")


def test_install_subscribes_its_hooks_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack.code_channels import hooks

    starts: list[tuple[Any, bool]] = []
    ends: list[Any] = []
    monkeypatch.setattr(hooks, "on_turn_start", lambda h, first=False: starts.append((h, first)))
    monkeypatch.setattr(hooks, "on_turn_end", lambda h: ends.append(h))
    monkeypatch.setattr(surface, "_INSTALLED", False)
    monkeypatch.setattr(surface, "_slack_provider", None)

    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    surface.install(first)
    surface.install(second)
    # Subscribed once; the later provider still replaces the earlier one.
    assert len(starts) == 3 and len(ends) == 1
    # The stop fence's turn mark and the wrap-up go first.
    assert [h for h, f in starts if f] == [wrapup.on_turn_start, surface.note_turn_start]
    assert surface.slack() == "second"


def test_a_slack_provider_that_raises_reads_as_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface, "_slack_provider", _boom)
    assert surface.slack() is None
    assert surface.client() is None
    monkeypatch.setattr(surface, "_slack_provider", None)
    assert surface.slack() is None


def test_dashboard_state_is_the_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack import handler

    marker = object()
    monkeypatch.setattr(handler, "get_dashboard_state", lambda: marker)
    assert surface.dashboard_state() is marker


def test_a_failed_audit_write_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[dict[str, Any]] = []

    class Sel:
        def log_api_access(self, **row: Any) -> None:
            rows.append(row)

    monkeypatch.setattr(surface, "sel", lambda: Sel())
    surface.audit("", "op", "allowed", "channel=C1")
    assert rows[0]["caller"] == "unknown" and rows[0]["source"] == "slack"
    monkeypatch.setattr(surface, "sel", _boom)
    surface.audit("U1", "op", "denied", "channel=C1", "why")  # does not raise


def test_caller_slot_key_is_empty_without_a_state_or_on_a_failed_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import session_control  # the facade mirrors every holder

    assert surface.caller_slot_key(None, "chat-1-1") == ""
    assert surface.caller_slot_key(object(), "") == ""
    monkeypatch.setattr(session_control, "caller_slot_key", lambda _s, key: f"slot:{key}")
    assert surface.caller_slot_key(object(), "chat-1-1") == "slot:chat-1-1"
    monkeypatch.setattr(session_control, "caller_slot_key", _boom)
    assert surface.caller_slot_key(object(), "chat-1-1") == ""


def test_no_slot_key_binds_no_channel() -> None:
    assert asyncio.run(surface.binding_for_slot(object(), "")) is None


def test_a_channel_not_fenced_here_is_never_fenced_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(surface, "_archived_here", set())
    assert asyncio.run(surface.fenced_unrecorded(_binding())) is False


def test_the_workspace_gate_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.platform as platform

    assert surface.workspace_admitted("") is False
    gate = SimpleNamespace(check_message_origin=lambda team: team == "T1")
    monkeypatch.setattr(platform, "current_context", lambda: SimpleNamespace(slack_gate=gate))
    assert surface.workspace_admitted("T1") is True
    assert surface.workspace_admitted("T2") is False
    monkeypatch.setattr(platform, "current_context", _boom)
    assert surface.workspace_admitted("T1") is False


def test_an_unreadable_channel_policy_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack import channel_policy

    monkeypatch.setattr(channel_policy, "channel_refusal", lambda *_a, **_k: "")
    assert surface.channel_policy_refusal("C1", operation="op", caller="U1") == ""
    monkeypatch.setattr(channel_policy, "channel_refusal", _boom)
    assert surface.channel_policy_refusal("C1", operation="op", caller="U1") == (
        "channel policy unreadable"
    )


def test_channel_stopped_reads_the_live_scopes_and_fails_to_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.slack.agent_sessions import fence

    live = [
        SimpleNamespace(channel="C1", stopped=True),
        SimpleNamespace(channel="C2", stopped=False),
    ]
    monkeypatch.setattr(fence, "_LIVE", live)
    assert surface.channel_stopped("C1") is True
    assert surface.channel_stopped("C2") is False

    class Broken:
        def __iter__(self) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr(fence, "_LIVE", Broken())
    assert surface.channel_stopped("C1") is False


def test_push_slots_calls_the_state_and_swallows_its_failure() -> None:
    pushed: list[bool] = []
    surface.push_slots(SimpleNamespace(push_slots_update=lambda: pushed.append(True)))
    assert pushed == [True]
    surface.push_slots(SimpleNamespace(push_slots_update=_boom))  # does not raise
    surface.push_slots(object())  # no push method: nothing to do
