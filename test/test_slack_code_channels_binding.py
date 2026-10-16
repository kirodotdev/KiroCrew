"""Slack Code channels: the bound dashboard session, the mirror bridges, turn hooks,
the native status subscriber, and the durable store.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import hooks
from kiro_crew.slack.code_channels import settings as cc_settings
from kiro_crew.slack.code_channels import status
from kiro_crew.slack.code_channels.binding import (
    SessionSpec,
    create_bound_session,
    folder_default_agent,
    mirror_post_thread,
    notify_turn_end,
    notify_turn_start,
)
from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.store import (
    ROUTE_CODE_CHANNEL,
    Binding,
    CodeChannelStore,
    Route,
    set_store,
)

BINDING = Binding(channel_id="C0CODE", team_id="T1", session_key="chat-1-1", anchor_ts="300.000001")


@pytest.fixture
def bound(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    store = CodeChannelStore(tmp_path / "slack-code-channels.json")
    store.put_binding(BINDING)
    set_store(store)
    toggle = {"on": True}
    cc_settings.note_granted_scopes([cc_settings.REQUIRED_SCOPE])
    monkeypatch.setattr(
        binding_mod,
        "live_config",
        lambda _orch=None: SimpleNamespace(slack=SimpleNamespace(code_channels=toggle["on"])),
    )
    yield toggle
    set_store(None)
    cc_settings.reset_capability()


# ── Store ────────────────────────────────────────────────────────────────────


def test_route_decisions_are_first_write_wins_and_destinations_monotonic(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    first = store.record_decision(Route("sid", "C1", "1.0", decision=ROUTE_CODE_CHANNEL, title="A"))
    again = store.record_decision(Route("sid", "C1", "1.0", decision="stay"))
    assert again == first
    assert store.record_destination("sid", "C9").channel_id == "C9"
    assert store.record_destination("sid", "C8").channel_id == "C9"
    reloaded = CodeChannelStore(tmp_path / "s.json")
    assert reloaded.get_route("sid").channel_id == "C9"
    assert reloaded.get_route("sid").decision == ROUTE_CODE_CHANNEL


def test_bindings_survive_a_restart_and_put_never_overwrites(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    other = Binding(channel_id="C0CODE", team_id="T1", session_key="chat-2-2", anchor_ts="1.0")
    assert store.put_binding(other).session_key == "chat-1-1"
    assert CodeChannelStore(tmp_path / "s.json").get_binding("C0CODE").session_key == "chat-1-1"
    store.replace_binding(other)
    assert CodeChannelStore(tmp_path / "s.json").get_binding("C0CODE").session_key == "chat-2-2"


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '{"bindings": {"C1": {"channel_id": "C2"}}}', "{}"]
)
def test_unreadable_or_mismatched_store_reads_as_empty(tmp_path: Any, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    store = CodeChannelStore(path)
    assert store.bindings() == [] and store.get_route("x") is None


def test_store_file_is_plain_json(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    data = json.loads((tmp_path / "s.json").read_text())
    assert data["bindings"]["C0CODE"]["anchor_ts"] == "300.000001"


# ── Mirror bridges ───────────────────────────────────────────────────────────


def test_mirror_posts_top_level_only_for_the_code_channels_own_session(
    bound: dict[str, bool],
) -> None:
    assert mirror_post_thread("C0CODE", "300.000001") is None
    # Another thread of the same channel, another channel, no channel: unchanged.
    assert mirror_post_thread("C0CODE", "999.0") == "999.0"
    assert mirror_post_thread("C0OTHER", "300.000001") == "300.000001"
    assert mirror_post_thread("", "5.0") == "5.0"


def test_mirror_is_unchanged_with_the_toggle_off(bound: dict[str, bool]) -> None:
    bound["on"] = False
    assert mirror_post_thread("C0CODE", "300.000001") == "300.000001"


@pytest.mark.asyncio
async def test_turn_hooks_fire_once_each_with_the_binding(bound: dict[str, bool]) -> None:
    seen: list[tuple[str, str, str, str]] = []

    async def on_start(b: Binding, e: hooks.TurnEvent) -> None:
        seen.append(("start", b.channel_id, e.source, e.outcome))

    async def on_end(b: Binding, e: hooks.TurnEvent) -> None:
        seen.append(("end", b.channel_id, e.source, e.outcome))

    hooks.on_turn_start(on_start)
    hooks.on_turn_start(on_start)  # idempotent
    hooks.on_turn_end(on_end)
    try:
        assert await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack") is True
        await notify_turn_end("C0CODE", "300.000001", "chat-1-1", source="slack", outcome="stopped")
        assert await notify_turn_start("C0OTHER", "1.0", "k", source="dashboard") is False
        bound["on"] = False
        assert await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack") is False
    finally:
        hooks.remove_hook(on_start)
        hooks.remove_hook(on_end)
    assert seen == [("start", "C0CODE", "slack", ""), ("end", "C0CODE", "slack", "stopped")]


@pytest.mark.asyncio
async def test_a_failing_or_stalled_hook_never_breaks_the_turn(
    bound: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT_SECS", 0.05)
    ran: list[str] = []

    async def boom(_b: Binding, _e: hooks.TurnEvent) -> None:
        raise RuntimeError("subscriber bug")

    async def stall(_b: Binding, _e: hooks.TurnEvent) -> None:
        await asyncio.sleep(10)

    async def after(_b: Binding, _e: hooks.TurnEvent) -> None:
        ran.append("after")

    for hook in (boom, stall, after):
        hooks.on_turn_start(hook)
    try:
        assert await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack") is True
    finally:
        for hook in (boom, stall, after):
            hooks.remove_hook(hook)
    assert ran == ["after"]


# ── Native status ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_is_processing_during_a_turn_and_active_after(bound: dict[str, bool]) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def api_call(method: str, **kwargs: Any) -> Any:
        calls.append((method, kwargs["json"]))
        return {"ok": True}

    status.set_client_factory(lambda: CodeChannelsClient(api_call))
    hooks.on_turn_start(status._on_start)
    hooks.on_turn_end(status._on_end)
    try:
        await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack")
        assert "C0CODE" in status._refreshers
        await notify_turn_end("C0CODE", "300.000001", "chat-1-1", source="slack", outcome="ok")
        assert "C0CODE" not in status._refreshers
    finally:
        hooks.remove_hook(status._on_start)
        hooks.remove_hook(status._on_end)
        status.set_client_factory(None)
    assert calls == [
        ("agents.sessions.setStatus", {"channel_id": "C0CODE", "status": "processing"}),
        ("agents.sessions.setStatus", {"channel_id": "C0CODE", "status": "active"}),
    ]


@pytest.mark.asyncio
async def test_status_write_failure_is_swallowed(bound: dict[str, bool]) -> None:
    class Err(Exception):
        response = {"ok": False, "error": "feature_disabled"}

    async def api_call(method: str, **kwargs: Any) -> Any:
        raise Err()

    status.set_client_factory(lambda: CodeChannelsClient(api_call))
    try:
        assert await status.set_status("C0CODE", "processing") is False
    finally:
        status.set_client_factory(None)


# ── Session settings ─────────────────────────────────────────────────────────


def test_folder_default_agent_walks_ancestors_and_survives_cycles() -> None:
    folders = [
        {"id": "a", "default_agent": "root-agent"},
        {"id": "b", "parent_id": "a"},
        {"id": "c", "parent_id": "b", "default_agent": " "},
        {"id": "x", "parent_id": "y"},
        {"id": "y", "parent_id": "x"},
    ]
    assert folder_default_agent(folders, "c") == "root-agent"
    assert folder_default_agent(folders, "x") == ""
    assert folder_default_agent(folders, "") == ""


@pytest.mark.asyncio
async def test_folder_project_dir_wins_over_the_origin_threads(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    state = SimpleNamespace(
        read_folders=_reader([{"id": "f1", "name": "F", "project_dir": str(project)}])
    )
    monkeypatch.setattr(binding_mod, "_origin_project", lambda ts: "/elsewhere")
    cfg = SimpleNamespace(agent=SimpleNamespace(default_agent=""))
    spec = await binding_mod.resolve_session_spec(
        state, cfg, binding_mod.CodeChannelDefaults(folder="f1"), origin_thread_ts="1.0"
    )
    assert spec.project_dir == str(project.resolve())


def _reader(folders: list[dict[str, Any]]):
    async def read(fn: Any) -> Any:
        return fn(folders)

    return read


# ── The real dashboard session ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bound_session_is_filed_pinned_titled_persisted_and_linked(tmp_path: Any) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    state._folders.append({"id": "f1", "name": "Slack work", "parent_id": "", "order": 0})
    spec = SessionSpec(model="claude-sonnet-4.5", reasoning_effort="high", folder_id="f1")
    slot = await create_bound_session(
        state, spec, title="Fix flaky login test", channel_id="C0CODE", anchor_ts="300.000001"
    )
    assert state.get_slot(slot.key) is slot
    assert slot.folder_id == "f1"
    assert slot.model == "claude-sonnet-4.5" and slot.reasoning_effort == "high"
    assert slot.title == "Fix flaky login test" and slot._titled
    assert isinstance(slot.project, str)  # the dashboard default (may be "" in tests)
    # Slack replies in the channel find this slot through the anchor.
    assert state.get_linked_slot("300.000001") is slot
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta and meta.get("folder_id") == "f1"


@pytest.mark.asyncio
async def test_bound_session_drops_an_unknown_effort(tmp_path: Any) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = await create_bound_session(
        state,
        SessionSpec(reasoning_effort="ludicrous"),
        title="t",
        channel_id="C1",
        anchor_ts="1.0",
    )
    assert slot.reasoning_effort in ("", None)
    assert slot.folder_id in ("", None)
