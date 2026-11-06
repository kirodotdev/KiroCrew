"""Slack Code channels: the bound dashboard session, the mirror bridges, turn hooks,
the native status subscriber, and the durable store.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.slack import features
from kiro_crew.slack.agent_sessions.status import AgentSessionStatus
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import hooks, status
from kiro_crew.slack.code_channels.binding import (
    SessionSpec,
    create_bound_session,
    mirror_post_thread,
    notify_turn_end,
    notify_turn_start,
)
from kiro_crew.slack.code_channels.store import (
    ROUTE_CODE_CHANNEL,
    Binding,
    CodeChannelStore,
    Route,
    StoreWriteError,
    set_store,
)

BINDING = Binding(channel_id="C0CODE", team_id="T1", session_key="chat-1-1", anchor_ts="300.000001")


@pytest.fixture
def bound(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    store = CodeChannelStore(tmp_path / "slack-code-channels.json")
    store.put_binding(BINDING)
    set_store(store)
    toggle = {"on": True}
    features.record_granted_scopes([features.REQUIRED_SCOPE[features.CODE_CHANNELS]])
    monkeypatch.setattr(
        binding_mod,
        "live_config",
        lambda _orch=None: SimpleNamespace(slack=SimpleNamespace(code_channels=toggle["on"])),
    )
    yield toggle
    set_store(None)
    features.reset_capabilities()


@pytest.mark.parametrize("toggle_on", [True, False])
def test_the_dashboards_lookups_never_load_a_cold_store_on_the_loop(
    bound: dict[str, bool], tmp_path: Any, monkeypatch: pytest.MonkeyPatch, toggle_on: bool
) -> None:
    """Before the Slack startup has loaded the store (off the loop), a linked
    dashboard turn's lookups answer "not a code channel" without the cold load:
    no file read, no wait on the loader's lock. Once loaded, they find it."""
    cold = CodeChannelStore(tmp_path / "slack-code-channels.json")  # BINDING is on disk
    set_store(cold)
    bound["on"] = toggle_on
    real_load = cold._ensure_loaded
    cold._ensure_loaded = lambda: pytest.fail("cold load on the loop")  # type: ignore[method-assign]
    real_peek = cold.get_binding_if_loaded
    if not toggle_on:  # feature off: the mirror helpers never touch the store
        cold.get_binding_if_loaded = lambda _c: pytest.fail("store read while off")  # type: ignore[method-assign]
    assert binding_mod.active_binding("C0CODE", "300.000001") is None
    assert binding_mod.mirror_post_thread("C0CODE", "300.000001") == "300.000001"
    started = asyncio.run(
        binding_mod.notify_turn_start("C0CODE", "300.000001", "k", source="dashboard")
    )
    assert started is False
    cold.get_binding_if_loaded = real_peek  # type: ignore[method-assign]
    asyncio.run(
        binding_mod.notify_turn_end("C0CODE", "300.000001", "k", source="dashboard", outcome="ok")
    )
    cold._ensure_loaded = real_load  # type: ignore[method-assign]
    cold.load()  # what the Slack startup does, off the loop
    expected = BINDING if toggle_on else None
    found = binding_mod.active_binding("C0CODE", "300.000001")
    assert (found.channel_id if found else None) == (expected.channel_id if expected else None)


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


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"bindings": {"C1": ',
        '{"bindings": "x"}',
        '{"routes": [1]}',
        "[]",
        '{"routes": {"s1": {"session_id": "s1", "origin_channel_id": "C1",'
        ' "origin_ts": "1.0", "created_at": "bad"}}}',
    ],
)
def test_an_unreadable_store_is_never_written_over(tmp_path: Any, content: str) -> None:
    """A store file that cannot be parsed reads as empty, but the next write
    must not replace it: it may hold every binding, route and revocation."""
    path = tmp_path / "s.json"
    path.write_text(content)
    store = CodeChannelStore(path)
    assert store.bindings() == []
    assert store.loaded and store.unreadable  # loads (as empty), never raises
    with pytest.raises(StoreWriteError):
        store.put_binding(BINDING)
    with pytest.raises(StoreWriteError):
        store.record_decision(Route("sid", "C1", "1.0", decision=ROUTE_CODE_CHANNEL))
    assert path.read_text() == content
    # A missing file is simply empty, and writable.
    fresh = CodeChannelStore(tmp_path / "new.json")
    assert fresh.put_binding(BINDING).channel_id == BINDING.channel_id


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


def test_routes_expire_and_are_capped(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.slack.code_channels import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_ROUTES", 3)
    store = CodeChannelStore(tmp_path / "s.json")
    now = time.time()
    old = now - store_mod.ROUTE_TTL_SECS - 1
    store.record_decision(Route("old", "C1", "1.0", decision="stay", created_at=old))
    for i in range(4):
        store.record_decision(Route(f"r{i}", "C1", f"{i}.0", decision="stay", created_at=now + i))
    kept = {sid for sid in ("old", "r0", "r1", "r2", "r3") if store.get_route(sid)}
    assert kept == {"r1", "r2", "r3"}  # expired dropped, then the oldest past the cap
    assert len(json.loads((tmp_path / "s.json").read_text())["routes"]) == 3


def test_past_the_cap_stay_routes_go_before_a_code_channel_route(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels import store as store_mod

    monkeypatch.setattr(store_mod, "MAX_ROUTES", 2)
    store = CodeChannelStore(tmp_path / "s.json")
    now = time.time()
    store.record_decision(Route("open", "C1", "0.0", decision="code_channel", created_at=now))
    for i in range(3):
        store.record_decision(
            Route(f"s{i}", "C1", f"{i + 1}.0", decision="stay", created_at=now + i)
        )
    # Thread replies record a stay row each; the oldest still-unfinished
    # code_channel row a restart needs is not the one evicted.
    assert store.get_route("open") is not None and store.get_route("s2") is not None


def test_a_thread_opened_a_channel_by_its_bindings_and_routes(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    assert not store.thread_opened_channel("C1", "10.0")
    store.record_decision(
        Route("r", "C1", "11.0", decision="code_channel", origin_thread_ts="10.0")
    )
    assert store.thread_opened_channel("C1", "10.0")
    assert not store.thread_opened_channel("C1", "10.0", except_ts="11.0")  # its own opener
    assert not store.thread_opened_channel("C2", "10.0")
    store.put_binding(
        Binding(
            channel_id="C0NEW",
            team_id="T1",
            session_key="chat-1-1",
            anchor_ts="1.0",
            origin_channel_id="C1",
            origin_ts="20.0",
        )
    )
    assert store.thread_opened_channel("C1", "20.0")  # a root mention's own thread
    reloaded = CodeChannelStore(tmp_path / "s.json")
    assert reloaded.thread_opened_channel("C1", "10.0")


def test_update_binding_changes_named_fields_atomically_and_persists(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    updated = store.update_binding("C0CODE", wrapped_up_at=12.5, channel_name="fix-login")
    assert updated is not None and updated.wrapped_up_at == 12.5
    assert updated.session_key == BINDING.session_key  # untouched fields kept
    reloaded = CodeChannelStore(tmp_path / "s.json").get_binding("C0CODE")
    assert reloaded is not None and reloaded.channel_name == "fix-login"
    assert reloaded.wrapped_up_at == 12.5 and reloaded.archived_at == 0.0
    assert store.update_binding("C0NONE", archived_at=1.0) is None


@pytest.mark.parametrize("bad", [{"channel_id": "C9"}, {"no_such_field": 1}])
def test_update_binding_refuses_unknown_fields_and_the_key(tmp_path: Any, bad: dict) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    with pytest.raises(TypeError):
        store.update_binding("C0CODE", **bad)
    assert store.get_binding("C0CODE") == CodeChannelStore(tmp_path / "s.json").get_binding(
        "C0CODE"
    )


@pytest.mark.parametrize(
    "write",
    [
        lambda s: s.put_binding(
            Binding(channel_id="C0NEW", team_id="T", session_key="k", anchor_ts="1.0")
        ),
        lambda s: s.update_binding("C0CODE", wrapped_up_at=1.0),
        lambda s: s.drop_binding("C0CODE"),
        lambda s: s.record_decision(Route("sid", "C1", "1.0", decision=ROUTE_CODE_CHANNEL)),
    ],
)
def test_a_failed_write_raises_and_changes_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, write: Any
) -> None:
    from kiro_crew.slack.code_channels import store as store_mod

    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store_mod, "atomic_write", boom)
    with pytest.raises(store_mod.StoreWriteError):
        write(store)
    # Nothing was published: memory still matches the file.
    assert store.get_binding("C0CODE") == _on_disk(store)
    assert store.get_binding("C0NEW") is None and store.get_route("sid") is None


def _on_disk(store: CodeChannelStore) -> Binding | None:
    return CodeChannelStore(store.path).get_binding("C0CODE")


def test_reads_are_lock_free_snapshots(tmp_path: Any) -> None:
    store = CodeChannelStore(tmp_path / "s.json")
    store.put_binding(BINDING)
    with store._lock:  # a writer holding the lock never blocks a reader
        assert store.get_binding("C0CODE") is not None
        assert store.bindings()


def test_a_record_written_before_the_new_fields_loads_unchanged(tmp_path: Any) -> None:
    path = tmp_path / "s.json"
    old = {
        "channel_id": "C0CODE",
        "team_id": "T1",
        "session_key": "chat-1-1",
        "anchor_ts": "300.000001",
        "created_at": 5.0,
    }
    path.write_text(json.dumps({"bindings": {"C0CODE": old}}))
    binding = CodeChannelStore(path).get_binding("C0CODE")
    assert binding is not None and binding.session_key == "chat-1-1"
    assert (binding.channel_name, binding.archived_at, binding.wrapped_up_at) == ("", 0.0, 0.0)


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


@pytest.mark.asyncio
async def test_a_system_notice_from_the_channels_session_posts_top_level(
    bound: dict[str, bool],
) -> None:
    """A system notice mirrored from the bound session (an expired sign-in) posts
    top-level in the code channel like every other post; elsewhere it keeps
    the linked thread."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard import chat_runner

    posted: list[Any] = []
    client = AsyncMock()
    client.post_message.side_effect = lambda *a, **_k: posted.append(a)
    state = SimpleNamespace(slack_client=client)
    for chan, thread, want in (("C0CODE", "300.000001", None), ("C0OTHER", "9.0", "9.0")):
        slot = SimpleNamespace(_slack_thread_ts=thread, _slack_channel=chan)
        await chat_runner._deliver_linked_slack_message(state, slot, None, "k", "signed out")
        assert posted[-1] == (chan, "signed out", want)


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
async def test_a_turn_cancelled_inside_its_start_hooks_still_owes_its_end(
    bound: dict[str, bool],
) -> None:
    """The owed end is recorded before any start subscriber is awaited, so a
    cancel landing inside one (a closed tab) still settles the channel status."""
    entered = asyncio.Event()
    owed: list[bool] = []

    async def slow(_b: Binding, _e: hooks.TurnEvent) -> None:
        entered.set()
        await asyncio.Event().wait()  # never set: the cancel ends it

    hooks.on_turn_start(slow)
    try:
        task = asyncio.create_task(
            notify_turn_start(
                "C0CODE",
                "300.000001",
                "chat-1-1",
                source="slack",
                on_bound=lambda: owed.append(True),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=30.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30.0)
        # Not bound: nothing is owed.
        assert (
            await notify_turn_start(
                "C0OTHER", "1.0", "k", source="dashboard", on_bound=lambda: owed.append(False)
            )
            is False
        )
    finally:
        hooks.remove_hook(slow)
    assert owed == [True]


@pytest.mark.asyncio
async def test_a_failing_or_stalled_hook_never_breaks_the_turn(
    bound: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_TIMEOUT_SECS", 0.05)
    ran: list[str] = []

    async def boom(_b: Binding, _e: hooks.TurnEvent) -> None:
        raise RuntimeError("subscriber bug")

    async def stall(_b: Binding, _e: hooks.TurnEvent) -> None:
        await asyncio.Event().wait()  # never set: only the hook timeout ends it

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


class _StatusSlack:
    """The two attributes the agent-sessions driver uses on a Slack client."""

    def __init__(self, answer: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._channel_team: dict[str, str] = {}
        self._answer = answer
        self._web = SimpleNamespace(api_call=self._api_call)

    async def _api_call(self, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, dict(kwargs["params"])))
        if isinstance(self._answer, BaseException):
            raise self._answer
        return {"ok": True}

    def _inject_team(self, channel: str, params: dict[str, Any]) -> None:
        return None


@pytest.mark.asyncio
async def test_status_is_processing_during_a_turn_and_active_after(bound: dict[str, bool]) -> None:
    # Thread agent sessions are off (no agent_sessions toggle, no assistant
    # scope): the channel's status follows the code-channel gate alone.
    slack = _StatusSlack()
    status.set_slack_provider(lambda: slack)
    hooks.on_turn_start(status._on_start)
    hooks.on_turn_end(status._on_end)
    try:
        await notify_turn_start("C0CODE", "300.000001", "chat-1-1", source="slack")
        await notify_turn_end("C0CODE", "300.000001", "chat-1-1", source="slack", outcome="ok")
    finally:
        hooks.remove_hook(status._on_start)
        hooks.remove_hook(status._on_end)
        status.set_slack_provider(None)
    assert slack.calls == [
        ("agents.sessions.setStatus", {"channel_id": "C0CODE", "status": "processing"}),
        ("agents.sessions.setStatus", {"channel_id": "C0CODE", "status": "active"}),
    ]
    # One driver per client, the shared agent-sessions one, not a second writer.
    assert isinstance(slack.code_channel_status, AgentSessionStatus)  # type: ignore[attr-defined]


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"ok": False, "error": code}


@pytest.mark.asyncio
@pytest.mark.parametrize("lift", ["reconnect", "check_again"])
async def test_a_status_refusal_is_lifted_when_slack_capabilities_are_reprobed(
    bound: dict[str, bool], lift: str
) -> None:
    """A ``feature_disabled`` status write latches only the code-channel driver;
    once the admin enables the feature, a reconnect or Check again re-probes and
    the next turn's status goes through again, without a gateway restart."""
    from kiro_crew.slack import features

    slack = _StatusSlack(answer=_Refused("feature_disabled"))
    status.set_slack_provider(lambda: slack)
    try:
        assert await status.set_status("C0CODE", "processing") is False
        slack._answer = None  # the admin enables the feature
        before = len(slack.calls)
        assert await status.set_status("C0CODE", "processing") is False  # still latched
        assert len(slack.calls) == before
        if lift == "reconnect":
            features.clear_latch()
        else:
            features.clear_private_latches()
        assert await status.set_status("C0CODE", "processing") is True
        assert slack.calls[-1] == (
            "agents.sessions.setStatus",
            {"channel_id": "C0CODE", "status": "processing"},
        )
        # Neither feature was latched off by the refusal.
        assert features.latched() == {}
    finally:
        status.set_slack_provider(None)


@pytest.mark.asyncio
async def test_a_reprobe_during_a_refused_status_write_still_lifts_its_latch(
    bound: dict[str, bool],
) -> None:
    """Check again lands while a write Slack will refuse is in flight: the latch
    that refusal sets belongs to the probe before, so the next write goes out."""
    from kiro_crew.slack import features

    class _ReprobedMidCall(_StatusSlack):
        async def _api_call(self, method: str, **kwargs: Any) -> Any:
            self.calls.append((method, dict(kwargs["params"])))
            if len(self.calls) == 1:
                features.clear_private_latches()  # the re-probe, mid-call
                raise _Refused("feature_disabled")
            return {"ok": True}

    slack = _ReprobedMidCall()
    status.set_slack_provider(lambda: slack)
    try:
        assert await status.set_status("C0CODE", "processing") is False
        assert await status.set_status("C0CODE", "processing") is True
        assert len(slack.calls) == 2
    finally:
        status.set_slack_provider(None)


@pytest.mark.asyncio
async def test_a_turn_ending_after_code_channels_went_off_still_clears_the_loader(
    bound: dict[str, bool],
) -> None:
    """The toggle is read live and the turn-end settle skips it, so a channel left
    on ``processing`` must still be settled to ``active`` when the switch goes off
    mid-turn -- otherwise Slack's loader sticks until its one-hour timeout. A new
    ``processing`` with the gate shut stays gated."""
    slack = _StatusSlack()
    status.set_slack_provider(lambda: slack)
    try:
        assert await status.set_status("C0CODE", "processing") is True
        bound["on"] = False  # operator turns code channels off during the turn
        # The turn-end settle (gate now closed) still clears the loader.
        assert await status.set_status("C0CODE", "active") is True
        assert slack.calls[-1] == (
            "agents.sessions.setStatus",
            {"channel_id": "C0CODE", "status": "active"},
        )
        # A fresh processing with the gate shut does not go through.
        before = len(slack.calls)
        assert await status.set_status("C0CODE", "processing") is False
        assert len(slack.calls) == before
    finally:
        status.set_slack_provider(None)


@pytest.mark.asyncio
async def test_settle_after_stop_clears_a_stuck_loader_with_the_gate_off(
    bound: dict[str, bool],
) -> None:
    slack = _StatusSlack()
    status.set_slack_provider(lambda: slack)
    try:
        assert await status.set_status("C0CODE", "processing") is True
        bound["on"] = False
        await status.settle_after_stop("C0CODE")
        assert slack.calls[-1] == (
            "agents.sessions.setStatus",
            {"channel_id": "C0CODE", "status": "active"},
        )
    finally:
        status.set_slack_provider(None)


@pytest.mark.asyncio
async def test_a_status_refusal_stops_only_code_channel_status(
    bound: dict[str, bool],
) -> None:
    """A ``missing_scope`` on a channel's status write stops that driver's status
    writes and latches NO feature: thread agent sessions stay as they were, and
    code channels themselves keep routing (status is decoration on the turn)."""
    from kiro_crew.slack import features

    refusal = Exception("missing_scope")
    refusal.response = {"error": "missing_scope"}  # type: ignore[attr-defined]
    slack = _StatusSlack(answer=refusal)
    status.set_slack_provider(lambda: slack)
    try:
        await status.set_status("C0CODE", "processing")
        latched = features.latched()
        assert features.CODE_CHANNELS not in latched
        assert features.AGENT_SESSIONS not in latched
        calls = len(slack.calls)
        assert calls >= 1
        await status.set_status("C0CODE", "processing")
        assert len(slack.calls) == calls  # this driver stopped writing
    finally:
        status.set_slack_provider(None)


@pytest.mark.asyncio
async def test_status_sends_nothing_with_the_toggle_off(bound: dict[str, bool]) -> None:
    bound["on"] = False
    slack = _StatusSlack()
    status.set_slack_provider(lambda: slack)
    try:
        assert await status.set_status("C0CODE", "processing") is False
        await status.settle_after_stop("C0CODE")
    finally:
        status.set_slack_provider(None)
    assert slack.calls == []


@pytest.mark.asyncio
async def test_status_write_failure_is_swallowed(bound: dict[str, bool]) -> None:
    class Err(Exception):
        response = {"ok": False, "error": "feature_disabled"}

    slack = _StatusSlack(Err())
    status.set_slack_provider(lambda: slack)
    try:
        assert await status.set_status("C0CODE", "processing") is False
    finally:
        status.set_slack_provider(None)
    assert len(slack.calls) == 1


# ── Session settings ─────────────────────────────────────────────────────────


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
    cfg = SimpleNamespace(
        agent=SimpleNamespace(default_agent=""),
        slack=SimpleNamespace(code_channel_defaults=SimpleNamespace(folder="f1")),
    )
    spec = await binding_mod.resolve_session_spec(state, cfg, origin_thread_ts="1.0")
    assert spec.project_dir == str(project.resolve())


@pytest.mark.asyncio
async def test_an_unusable_folder_project_falls_back_to_the_origin_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SimpleNamespace(
        read_folders=_reader([{"id": "f1", "name": "F", "project_dir": "/no/such/dir"}])
    )
    monkeypatch.setattr(binding_mod, "_origin_project", lambda ts: "/work/origin-repo")
    cfg = SimpleNamespace(
        agent=SimpleNamespace(default_agent=""),
        slack=SimpleNamespace(code_channel_defaults=SimpleNamespace(folder="f1")),
    )
    spec = await binding_mod.resolve_session_spec(state, cfg, origin_thread_ts="1.0")
    assert spec.folder_id == "f1" and spec.project_dir == "/work/origin-repo"


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
    # The map write is the debounced, off-loop one, awaited before returning.
    state.sessions.aflush.assert_awaited()
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta and meta.get("folder_id") == "f1"


@pytest.mark.asyncio
async def test_a_picked_template_is_never_bound_as_a_same_named_crewmate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crewmate enrolled under the template's name owns its own memory and
    model pin: the defaults picker's template pick must not resolve to it."""
    from chat_test_helpers import _make_state

    from kiro_crew.config import loader
    from kiro_crew.dashboard import chat_handlers

    real = loader.resolve_agent_bindings
    kinds: list[str] = []

    def resolve(cfg: Any, agent: str, project: Any, **kw: Any) -> Any:
        kinds.append(kw.get("selection_kind", ""))
        return real(cfg, agent, project, **kw)

    pinned: list[str] = []

    async def pin(_state: Any, _key: str, agent: str, *_a: Any, **_k: Any) -> str:
        pinned.append(agent)  # a crewmate's private store
        return "crewmate-store"

    monkeypatch.setattr(loader, "resolve_agent_bindings", resolve)
    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    state = _make_state(tmp_path)
    spec = SessionSpec(agent="builder", agent_kind="template")
    slot = await create_bound_session(
        state, spec, title="Fix it", channel_id="C0CODE", anchor_ts="300.000001"
    )
    assert kinds and set(kinds) == {"template"}
    assert pinned == [] and slot.memory_store != "crewmate-store"
    assert slot.agent == "builder" and slot.agent_kind == "template"


@pytest.mark.asyncio
async def test_birth_fields_merge_into_metadata_the_agent_selection_already_wrote(
    tmp_path: Any,
) -> None:
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    state = _make_state(tmp_path)
    state._folders.append({"id": "f1", "name": "Slack work", "parent_id": "", "order": 0})
    real_update_if = state.conversation_log.update_metadata_if
    calls: list[str] = []

    def first_write_refused(key: str, line: Any, pred: Any, **kw: Any) -> bool:
        if not calls:
            # The agent selection wrote execution metadata first, so the
            # "only when empty" birth write is refused.
            calls.append(key)
            state.conversation_log.update_metadata(key, {"execution_context": {"kind": "template"}})
        return real_update_if(key, line, pred, **kw)

    state.conversation_log.update_metadata_if = first_write_refused
    spec = SessionSpec(model="claude-sonnet-4.5", reasoning_effort="high", folder_id="f1")
    slot = await create_bound_session(
        state, spec, title="Fix flaky login test", channel_id="C0CODE", anchor_ts="300.000001"
    )
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("execution_context") == {"kind": "template"}
    assert meta.get("folder_id") == "f1" and meta.get("title") == "Fix flaky login test"


@pytest.mark.asyncio
async def test_a_session_whose_birth_cannot_be_saved_is_retracted(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chat_test_helpers import _make_state

    import kiro_crew.dashboard.chat_handlers as chat_handlers

    state = _make_state(tmp_path)
    state.conversation_log.update_metadata_if = lambda *_a, **_k: False

    async def save_fails(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save_fails)
    before = set(state._slots)
    with pytest.raises(binding_mod.BindingUnavailable):
        await create_bound_session(
            state, SessionSpec(), title="t", channel_id="C1", anchor_ts="1.0"
        )
    assert set(state._slots) == before
    assert state.get_linked_slot("1.0") is None


@pytest.mark.asyncio
async def test_a_birth_save_that_raises_retracts_the_session(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chat_test_helpers import _make_state

    import kiro_crew.dashboard.chat_handlers as chat_handlers

    state = _make_state(tmp_path)
    state.conversation_log.update_metadata_if = lambda *_a, **_k: False

    async def save_raises(*_a: Any, **_k: Any) -> bool:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save_raises)
    before = set(state._slots)
    with pytest.raises(OSError):
        await create_bound_session(
            state, SessionSpec(), title="t", channel_id="C1", anchor_ts="1.0"
        )
    assert set(state._slots) == before
    assert state.get_linked_slot("1.0") is None


@pytest.mark.asyncio
async def test_a_default_project_is_resolved_off_the_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from types import SimpleNamespace

    from chat_test_helpers import _make_state

    from kiro_crew.config import KiroCrewConfig

    project = tmp_path / "proj"
    project.mkdir()
    real_load = KiroCrewConfig.load

    def load_with_default(*a: Any, **k: Any) -> Any:
        cfg = real_load(*a, **k)
        cfg.dashboard = SimpleNamespace(**{**vars(cfg.dashboard), "default_project": str(project)})
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(load_with_default))
    on_loop: list[bool] = []
    real = binding_mod._usable_project_dir

    def spy(configured: str) -> str:
        on_loop.append(threading.current_thread() is threading.main_thread())
        return real(configured)

    monkeypatch.setattr(binding_mod, "_usable_project_dir", spy)
    slot = await create_bound_session(
        _make_state(tmp_path), SessionSpec(), title="t", channel_id="C1", anchor_ts="1.0"
    )
    assert on_loop == [False]
    assert slot.project == str(project.resolve())


@pytest.mark.asyncio
async def test_a_failed_binding_write_or_flush_takes_the_linked_session_down(
    tmp_path: Any,
) -> None:
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels import store as store_mod

    for failure in ("binding", "flush"):
        state = _make_state(tmp_path / failure)
        before = set(state._slots)

        async def flush_fails() -> None:
            raise OSError(28, "No space left on device")

        async def binding_fails(_slot: Any) -> None:
            raise store_mod.StoreWriteError("disk full")

        if failure == "flush":
            state.sessions.aflush = flush_fails
        with pytest.raises((OSError, store_mod.StoreWriteError)):
            await create_bound_session(
                state,
                SessionSpec(),
                title="t",
                channel_id="C1",
                anchor_ts="7.0",
                before_publish=binding_fails if failure == "binding" else None,
            )
        # Not in the slot set (so no update can show it), not linked, and the
        # persisted link cleared.
        assert set(state._slots) == before, failure
        assert state.get_linked_slot("7.0") is None, failure
        assert "7.0" not in state._slack_to_slot, failure
        assert state.sessions.clear_slack_link.called, failure
        # The anchor held for the bind is released: nothing is left owning it.
        assert binding_mod.anchor_owner_by_ts("7.0") is None, failure


@pytest.mark.asyncio
async def test_a_link_landing_before_the_binding_is_stored_is_refused(
    tmp_path: Any,
) -> None:
    """Before the binding is stored the anchor is held for the newborn, so a
    rival link in that window is refused, and after it the binding refuses it."""
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels import store as store_mod

    state = _make_state(tmp_path)
    rival = state.get_or_create_slot("rival")
    cc = store_mod.CodeChannelStore(tmp_path / "cc.json")
    store_mod.set_store(cc)
    try:

        async def record_binding(slot: Any) -> None:
            with pytest.raises(binding_mod.AnchorHandoffRefused):
                state.link_slack(rival.key, "9.0", "C1")  # inside the window
            cc.put_binding(
                store_mod.Binding(
                    channel_id="C1", team_id="T1", session_key=slot.key, anchor_ts="9.0"
                )
            )

        # The anchor is held from the post on (routing's _bind_channel).
        with binding_mod.hold_anchor("9.0"):
            with pytest.raises(binding_mod.AnchorHandoffRefused):
                state.link_slack(rival.key, "9.0", "C1")  # before the session exists
            slot = await create_bound_session(
                state,
                SessionSpec(),
                title="t",
                channel_id="C1",
                anchor_ts="9.0",
                before_publish=record_binding,
            )
        assert state.get_linked_slot("9.0") is slot
        assert not rival._slack_linked
        with pytest.raises(binding_mod.AnchorHandoffRefused):
            state.link_slack(rival.key, "9.0", "C1")
    finally:
        store_mod.set_store(None)


@pytest.mark.asyncio
async def test_a_failed_bind_releases_its_anchor_hold(tmp_path: Any) -> None:
    """A bind whose binding write fails leaves no hold behind: the session and
    its link are retracted, and the anchor is an ordinary thread again."""
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels import store as store_mod

    state = _make_state(tmp_path)
    other = state.get_or_create_slot("other")

    async def binding_fails(_slot: Any) -> None:
        raise store_mod.StoreWriteError("disk full")

    with pytest.raises(store_mod.StoreWriteError):
        with binding_mod.hold_anchor("8.0"):
            await create_bound_session(
                state,
                SessionSpec(),
                title="t",
                channel_id="C1",
                anchor_ts="8.0",
                before_publish=binding_fails,
            )
    assert binding_mod.anchor_owner_by_ts("8.0") is None
    state.link_slack(other.key, "8.0", "C1")
    assert state.get_linked_slot("8.0") is other


@pytest.mark.asyncio
async def test_a_fresh_home_resolves_to_the_conductor_in_code_channels(tmp_path: Any) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    cfg = SimpleNamespace(
        slack=SimpleNamespace(code_channels=True, code_channel_defaults=None),
        agent=SimpleNamespace(default_agent="slack-default"),
    )
    spec = await binding_mod.resolve_session_spec(state, cfg)
    [folder] = [f for f in state._folders if f.get("name") == "Code channels"]
    assert spec.folder_id == folder["id"] and spec.agent == "kirocrew-conductor"
    # The next channel files into the same folder.
    assert (await binding_mod.resolve_session_spec(state, cfg)).folder_id == folder["id"]
    assert len([f for f in state._folders if f.get("name") == "Code channels"]) == 1


@pytest.mark.asyncio
async def test_two_code_channels_folders_file_the_session_nowhere(tmp_path: Any) -> None:
    """Two top-level "Code channels" folders are ambiguous, as the settings page
    previews them: the session is unfiled on Slack's default agent, never filed
    into an arbitrary one with its agent and project, and no third is created."""
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    for fid, agent, project in (("a" * 12, "agent-a", "/repo/a"), ("b" * 12, "agent-b", "/b")):
        state._folders.append(
            {
                "id": fid,
                "name": "Code channels",
                "parent_id": "",
                "default_agent": agent,
                "project_dir": project,
            }
        )
    cfg = SimpleNamespace(
        slack=SimpleNamespace(code_channels=True, code_channel_defaults=None),
        agent=SimpleNamespace(default_agent="slack-default"),
    )
    spec = await binding_mod.resolve_session_spec(state, cfg)
    assert spec.folder_id == "" and spec.agent == "slack-default"
    assert len([f for f in state._folders if f.get("name") == "Code channels"]) == 2
    with pytest.raises(binding_mod.BindingUnavailable):
        await binding_mod.ensure_folder_path(state, binding_mod.AUTO_FOLDER_PATH)


@pytest.mark.asyncio
async def test_code_channels_folder_is_created_once_reused_and_recreated(tmp_path: Any) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    assert not [f for f in state._folders if f.get("name") == "Code channels"]
    first = await binding_mod.ensure_folder_path(
        state, binding_mod.AUTO_FOLDER_PATH, default_agent=binding_mod.AUTO_FOLDER_AGENT
    )
    [folder] = [f for f in state._folders if f["id"] == first]
    assert folder["name"] == "Code channels" and folder["default_agent"] == "kirocrew-conductor"
    # A second channel reuses it: no duplicate.
    assert await binding_mod.ensure_folder_path(state, "Code channels") == first
    assert len([f for f in state._folders if f.get("name") == "Code channels"]) == 1
    # The user deleted it: the next channel recreates it.
    state._folders[:] = [f for f in state._folders if f["id"] != first]
    again = await binding_mod.ensure_folder_path(
        state, binding_mod.AUTO_FOLDER_PATH, default_agent=binding_mod.AUTO_FOLDER_AGENT
    )
    assert again != first and [f["name"] for f in state._folders if f["id"] == again] == [
        "Code channels"
    ]
    # A nested path (a per-channel subfolder) is mkdir -p under it.
    child = await binding_mod.ensure_folder_path(state, "Code channels/fix-login")
    [sub] = [f for f in state._folders if f["id"] == child]
    assert sub["parent_id"] == again and sub["name"] == "fix-login"
    assert not sub.get("default_agent")


@pytest.mark.asyncio
async def test_folder_auto_create_is_sel_audited_and_keeps_a_person_set_agent(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chat_test_helpers import _make_state

    import kiro_crew.sel as sel_mod

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sel_mod,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    state = _make_state(tmp_path)
    created = await binding_mod.ensure_folder_path(
        state, binding_mod.AUTO_FOLDER_PATH, default_agent=binding_mod.AUTO_FOLDER_AGENT
    )
    assert events == [
        {
            "caller": "slack-code-channels",
            "operation": "folder_auto_create",
            "outcome": "allowed",
            "source": "slack",
            "resources": created,
        }
    ]
    # Reuse writes nothing and audits nothing.
    await binding_mod.ensure_folder_path(
        state, binding_mod.AUTO_FOLDER_PATH, default_agent=binding_mod.AUTO_FOLDER_AGENT
    )
    assert len(events) == 1
    # A folder whose default agent a person set keeps it.
    [folder] = [f for f in state._folders if f["id"] == created]
    folder["default_agent"] = "my-agent"
    assert (
        await binding_mod.ensure_folder_path(
            state, binding_mod.AUTO_FOLDER_PATH, default_agent=binding_mod.AUTO_FOLDER_AGENT
        )
        == created
    )
    assert folder["default_agent"] == "my-agent" and len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["kirocrew-conductor", "kirocrew-team-lead"])
async def test_a_conductor_default_agent_binds_the_session_explicitly(
    tmp_path: Any, agent: str
) -> None:
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    state = _make_state(tmp_path)
    folder_id = await binding_mod.ensure_folder_path(state, "Code channels", default_agent=agent)
    spec = SessionSpec(
        agent=agent, model="claude-sonnet-4.5", reasoning_effort="high", folder_id=folder_id
    )
    slot = await create_bound_session(
        state, spec, title="Fix login", channel_id="C0CODE", anchor_ts="400.000001"
    )
    # Bound to that agent by name (persisted), never an inherited or omitted agent;
    # model and effort pass through.
    assert slot.agent == agent
    assert slot.reasoning_effort == "high" and slot.folder_id == folder_id
    meta = state.conversation_log.get_metadata(slot_history_key(slot))
    assert meta.get("folder_id") == folder_id and meta.get("agent") == agent
    assert meta.get("model") == "claude-sonnet-4.5"
    assert state.get_linked_slot("400.000001") is slot


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
