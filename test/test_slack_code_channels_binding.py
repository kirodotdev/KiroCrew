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
