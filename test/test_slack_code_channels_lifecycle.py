"""Archive, unarchive and rename events keep a code channel's binding current."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack.code_channels import lifecycle
from kiro_crew.slack.code_channels.store import Binding, CodeChannelStore, set_store


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    store = CodeChannelStore(tmp_path / "slack-code-channels.json")
    set_store(store)
    sel = _Sel()
    pushes: list[int] = []
    monkeypatch.setattr(lifecycle, "sel", lambda: sel)
    monkeypatch.setattr(lifecycle, "_push_slots", lambda: pushes.append(1))
    admitted = {"T1", "T2"}
    gate = SimpleNamespace(check_message_origin=lambda team: team in admitted)
    monkeypatch.setattr(lifecycle, "current_context", lambda: SimpleNamespace(slack_gate=gate))
    feature = {"on": True}
    monkeypatch.setattr(lifecycle, "_gate_open", lambda: feature["on"])
    store.put_binding(
        Binding(channel_id="C0CODE1", team_id="T1", session_key="chat-1-1", anchor_ts="200.1")
    )

    class Env:
        pass

    e = Env()
    e.store, e.sel, e.pushes, e.feature = store, sel, pushes, feature  # type: ignore[attr-defined]
    yield e
    set_store(None)


def _payload(team: str = "T1") -> dict[str, Any]:
    return {"team_id": team} if team else {}


@pytest.mark.asyncio
async def test_archive_then_unarchive(env: Any) -> None:
    ok = await lifecycle.handle_lifecycle_event(
        _payload(), {"type": "channel_archive", "channel": "C0CODE1", "user": "U1"}
    )
    assert ok and env.store.get_binding("C0CODE1").archived_at > 0
    ok = await lifecycle.handle_lifecycle_event(
        _payload(), {"type": "group_unarchive", "channel": "C0CODE1"}
    )
    assert ok and env.store.get_binding("C0CODE1").archived_at == 0
    assert [r["outcome"] for r in env.sel.rows] == ["allowed", "allowed"]
    assert all(r["operation"] == "slack.code_channel.lifecycle" for r in env.sel.rows)
    assert len(env.pushes) == 2


@pytest.mark.asyncio
async def test_events_for_one_channel_apply_in_delivery_order(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    real_apply = lifecycle._apply
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def slow_archive(binding: Any, changes: dict[str, Any]) -> Any:
        # The archive's store write is held open: unordered handlers would let
        # the unarchive behind it land first, then be overwritten by it.
        if changes.get("archived_at"):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(30)
        return real_apply(binding, changes)

    monkeypatch.setattr(lifecycle, "_apply", slow_archive)
    first = asyncio.ensure_future(
        lifecycle.handle_lifecycle_event(
            _payload(), {"type": "channel_archive", "channel": "C0CODE1"}
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=30)
    second = asyncio.ensure_future(
        lifecycle.handle_lifecycle_event(
            _payload(), {"type": "channel_unarchive", "channel": "C0CODE1"}
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    # The unarchive is parked on the channel's lock behind the archive.
    assert lifecycle._lock_users["C0CODE1"] == 2 and lifecycle._channel_locks["C0CODE1"].locked()
    assert not second.done()
    release.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert env.store.get_binding("C0CODE1").archived_at == 0
    assert lifecycle._channel_locks == {} and lifecycle._lock_users == {}


@pytest.mark.asyncio
async def test_rename_sets_the_channel_name(env: Any) -> None:
    event = {"type": "channel_rename", "channel": {"id": "C0CODE1", "name": "fix-login-v2"}}
    assert await lifecycle.handle_lifecycle_event(_payload(), event)
    assert env.store.get_binding("C0CODE1").channel_name == "fix-login-v2"


@pytest.mark.asyncio
async def test_a_rename_with_no_name_changes_nothing(env: Any) -> None:
    event = {"type": "channel_rename", "channel": {"id": "C0CODE1", "name": "  "}}
    assert not await lifecycle.handle_lifecycle_event(_payload(), event)
    assert env.pushes == []


@pytest.mark.asyncio
async def test_an_unbound_channel_is_ignored_silently(env: Any) -> None:
    event = {"type": "channel_archive", "channel": "C0OTHER"}
    assert not await lifecycle.handle_lifecycle_event(_payload(), event)
    assert env.sel.rows == [] and env.pushes == []


@pytest.mark.asyncio
async def test_missing_workspace_is_refused_and_audited(env: Any) -> None:
    event = {"type": "channel_archive", "channel": "C0CODE1", "user": "U1"}
    assert not await lifecycle.handle_lifecycle_event(_payload(""), event)
    assert [(r["outcome"], r["error"]) for r in env.sel.rows] == [("denied", "missing_team_id")]
    assert env.pushes == []


@pytest.mark.asyncio
async def test_with_the_feature_off_nothing_changes(env: Any) -> None:
    env.feature["on"] = False
    event = {"type": "channel_archive", "channel": "C0CODE1"}
    assert not await lifecycle.handle_lifecycle_event(_payload(), event)
    assert env.store.get_binding("C0CODE1").archived_at == 0
    assert env.sel.rows == [] and env.pushes == []


@pytest.mark.asyncio
async def test_a_binding_with_no_recorded_workspace_matches_no_event(env: Any) -> None:
    env.store.put_binding(
        Binding(channel_id="C0BLANK", team_id="", session_key="chat-2-1", anchor_ts="200.2")
    )
    event = {"type": "group_archive", "channel": "C0BLANK"}
    assert not await lifecycle.handle_lifecycle_event(_payload("T1"), event)
    assert [(r["outcome"], r["error"]) for r in env.sel.rows] == [("denied", "workspace_mismatch")]
    assert env.store.get_binding("C0BLANK").archived_at == 0


@pytest.mark.asyncio
async def test_a_workspace_the_origin_gate_refuses_is_refused_and_audited(env: Any) -> None:
    event = {"type": "channel_archive", "channel": "C0CODE1"}
    assert not await lifecycle.handle_lifecycle_event(_payload("T9"), event)
    assert [(r["outcome"], r["error"]) for r in env.sel.rows] == [
        ("denied", "workspace_not_allowed")
    ]
    assert env.store.get_binding("C0CODE1").archived_at == 0 and env.pushes == []


@pytest.mark.asyncio
async def test_another_workspace_is_refused_and_audited(env: Any) -> None:
    event = {"type": "channel_archive", "channel": "C0CODE1"}
    assert not await lifecycle.handle_lifecycle_event(_payload("T2"), event)
    assert [(r["outcome"], r["error"]) for r in env.sel.rows] == [("denied", "workspace_mismatch")]
    assert env.store.get_binding("C0CODE1").archived_at == 0


@pytest.mark.asyncio
async def test_other_event_types_are_not_handled(env: Any) -> None:
    assert not await lifecycle.handle_lifecycle_event(_payload(), {"type": "message"})


def test_every_lifecycle_event_is_subscribed_under_the_code_channels_switch() -> None:
    from kiro_crew import slack_manifest

    events = slack_manifest.requirements(frozenset({"code_channels"})).bot_events
    assert lifecycle.LIFECYCLE_EVENTS <= events
    assert not lifecycle.LIFECYCLE_EVENTS & slack_manifest.requirements(frozenset()).bot_events
