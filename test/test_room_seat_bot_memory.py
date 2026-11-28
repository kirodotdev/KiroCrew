"""A Bot in a Channels room reads and writes its own memory store, never Global.

A seat runs under ``channel:<room>:<seat>``. A seat that runs a Bot with its own
store is bound to that Bot's execution before it starts and before each turn, so
every memory and lesson tool it calls works against the store the Bot has now. A
template seat binds nothing and is never an established session, so it gets no
memory at all.

Every event that changes which store a seat's Bot has is a row below, with its
own test: the seat never reaches a store that is not its Bot's now.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from member_memory_helpers import declare_v2_store
from member_memory_helpers import env as _member_env
from member_memory_helpers import forget_declared_stores, make_request, member_manifest

from kiro_crew import context
from kiro_crew.channel import ChannelManager, run_channel_agent
from kiro_crew.dashboard.handlers import cron, memory_member
from kiro_crew.learn import Lesson, LessonStore

env = _member_env

BOT = "alice"
BOT_STORE = "member-alice"
#: A second store of the Bot's own, which a reassignment moves it to.
NEXT_STORE = "member-alice-next"
GLOBAL_MARKER = "global owner lesson kumquat"
ROOM_MARKER = "room seat lesson zebra"


class _Sessions:
    """A session manager whose seat start stops at the claim, then reports the seat live."""

    def __init__(self) -> None:
        self.claimed: list[str] = []

    async def get_or_create(self, key, **kwargs):
        self.claimed.append(key)
        raise RuntimeError("stop after the claim")

    def has_session(self, key) -> bool:
        return key in self.claimed

    def mark_lifecycle_lease(self, key) -> None:
        pass

    def set_lifecycle_turn_active(self, key, active) -> None:
        pass

    def release(self, key) -> None:
        pass


@pytest.fixture
def room(env, tmp_path, monkeypatch):
    monkeypatch.setattr(context, "_vector_stores", dict(env.tiers))
    monkeypatch.setattr(context, "_memory_stores", {})
    env.state.push_refresh = lambda _resource: None
    env.state.get_slot = lambda *_args, **_kwargs: None
    env.state._background_tasks = set()
    env.state.lessons = LessonStore(base_dir=tmp_path / "global-lessons")
    env.state.lessons.save(
        Lesson(rule=GLOBAL_MARKER, category="knowledge", ts="2026-11-28T00:00:00Z")
    )
    env.state.sessions = _Sessions()
    manager = ChannelManager(channels_dir=str(tmp_path / "channels"))
    env.state.channel_manager = manager
    return manager.create("incident")


async def _start(env, room, agent_name: str):
    seat = room.add_agent(role="Seat", agent_name=agent_name, task="t")
    await run_channel_agent(seat, room, env.state.sessions)
    return seat


def _call(env, key: str, path: str, **kwargs):
    return make_request(env.state, path, session=key, internal=True, **kwargs)


@pytest.mark.asyncio
async def test_a_bot_seat_learns_and_recalls_in_its_own_store(env, room):
    seat = await _start(env, room, BOT)
    global_vector_before = env.tiers[""].count_lessons()

    added = await cron.api_lessons_create(
        _call(env, seat.session_key, "/api/lessons", body={"rule": ROOM_MARKER})
    )
    listed = await cron.api_lessons(_call(env, seat.session_key, "/api/lessons"))
    recalled = await memory_member.api_memory_recall(
        _call(env, seat.session_key, "/api/memory/recall", query={"q": ROOM_MARKER})
    )

    assert added.status == 200, added.text
    assert listed.status == 200, listed.text
    rules = [row["rule"] for row in json.loads(listed.text)["lessons"]]
    assert ROOM_MARKER in rules and GLOBAL_MARKER not in rules
    assert recalled.status == 200, recalled.text
    assert ROOM_MARKER in recalled.text and GLOBAL_MARKER not in recalled.text
    assert ROOM_MARKER in json.dumps(env.tiers[BOT_STORE].get_lessons())
    assert env.tiers[""].count_lessons() == global_vector_before
    assert [lesson.rule for lesson in env.state.lessons.load_all()] == [GLOBAL_MARKER]


@pytest.mark.asyncio
async def test_a_template_seat_gets_no_memory_and_never_the_global_lessons(env, room):
    seat = await _start(env, room, "")

    responses = [
        await cron.api_lessons(_call(env, seat.session_key, "/api/lessons")),
        await cron.api_lessons_create(
            _call(env, seat.session_key, "/api/lessons", body={"rule": ROOM_MARKER})
        ),
        await memory_member.api_memory_recall(
            _call(env, seat.session_key, "/api/memory/recall", query={"q": GLOBAL_MARKER})
        ),
    ]

    for response in responses:
        assert response.status == 400, response.text
        assert json.loads(response.text)["code"] == "unknown_session"
        assert GLOBAL_MARKER not in response.text
    assert [lesson.rule for lesson in env.state.lessons.load_all()] == [GLOBAL_MARKER]


@pytest.mark.asyncio
async def test_a_bot_whose_store_cannot_be_bound_never_starts(env, room, monkeypatch):
    from kiro_crew import channel_seat_memory
    from kiro_crew.memory_stores import UnknownMemoryStore

    def refuse(*_args):
        raise UnknownMemoryStore("member identity is missing or ambiguous")

    monkeypatch.setattr(channel_seat_memory, "bind_seat_memory", refuse)
    seat = await _start(env, room, BOT)

    assert env.state.sessions.claimed == []
    assert seat.state == "failed"
    assert any(BOT in m.content and m.msg_type == "system" for m in room.messages)


@pytest.mark.asyncio
async def test_a_bot_on_a_legacy_v1_store_is_recognized_in_its_seat(env, room):
    """A V1 named store carries no member id; the seat is still its Bot's own."""
    from kiro_crew.config.loader import _invalidate_config_cache
    from kiro_crew.execution_context import read_session_execution

    config_file = env.home / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    (env.home / "memory_stores" / "team-notes").mkdir(parents=True)
    config["memory_stores"]["team-notes"] = {"memory_version": 1}
    config["agents"]["carol"] = {"memory_store": "team-notes"}
    config_file.write_text(json.dumps(config), encoding="utf-8")
    _invalidate_config_cache()

    seat = await _start(env, room, "carol")
    execution = read_session_execution(seat.session_key)

    assert execution.member_id is None and execution.store.legacy_name == "team-notes"
    # Bound, then claimed: the fake session manager stops every seat at its claim.
    assert env.state.sessions.claimed == [seat.session_key]
    refusal = await cron._recognize_session(
        env.state, seat.session_key, "test", blocks_persisted_mode=lambda _mode: False
    )
    assert refusal is None


@pytest.mark.asyncio
async def test_a_cron_run_still_lists_lessons(env, room):
    """Only room seats are gated: another keyed agent caller keeps its list."""
    response = await cron.api_lessons(_call(env, "cron:job1:run1", "/api/lessons"))

    assert response.status == 200, response.text
    assert [row["rule"] for row in json.loads(response.text)["lessons"]] == [GLOBAL_MARKER]


def test_a_bot_seat_record_names_the_bots_store(env, room):
    from kiro_crew.channel_seat_memory import bind_seat_memory, seat_has_own_store
    from kiro_crew.execution_context import read_session_execution

    seat = room.add_agent(role="Seat", agent_name=BOT, task="t")
    assert bind_seat_memory(BOT, seat.session_key) is True
    assert read_session_execution(seat.session_key).store.legacy_name == BOT_STORE
    assert seat_has_own_store(seat.session_key, BOT)
    template = room.add_agent(role="Helper", agent_name="", task="t")
    assert bind_seat_memory("", template.session_key) is False
    assert not seat_has_own_store(template.session_key, "")
    assert not seat_has_own_store("dashboard:alice", BOT)


# ── events that change a seat's Bot's store ──────────────────────────────────────


def _edit_config(env, monkeypatch, edit) -> None:
    """Apply *edit* to the home's ``config.json`` the way a hand edit lands."""
    path = env.home / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    edit(config)
    path.write_text(json.dumps(config), encoding="utf-8")
    forget_declared_stores(monkeypatch)


def _open_next_store(env) -> None:
    """Provision the Bot's second store and route its tier the way the first one is."""
    from kiro_crew.vector_memory import open_member_database

    directory = declare_v2_store(env.home, NEXT_STORE, BOT)
    tier = open_member_database(directory / "memory.db", member_id=BOT, store_id=NEXT_STORE)
    env.tiers[NEXT_STORE] = tier
    context._vector_stores[NEXT_STORE] = tier


def _reassign(config) -> None:
    config["memory_stores"][NEXT_STORE] = member_manifest(BOT)
    config["agents"][BOT]["memory_store"] = NEXT_STORE


async def _learn(env, seat, rule: str):
    return await cron.api_lessons_create(
        _call(env, seat.session_key, "/api/lessons", body={"rule": rule})
    )


def _stores_holding(env, rule: str) -> list[str]:
    """Every store whose lessons hold *rule*, the Global ones included."""
    stores = [
        name or "Global" for name, tier in env.tiers.items() if rule in str(tier.get_lessons())
    ]
    if rule in [lesson.rule for lesson in env.state.lessons.load_all()]:
        stores.append("Global lessons")
    return sorted(stores)


async def _next_turn(seat) -> None:
    """What the room loop runs for a seat before each turn."""
    from kiro_crew.channel_seat_memory import refresh_seat_memory

    await refresh_seat_memory(seat)


def _assert_refused(response) -> None:
    assert response.status == 400, response.text
    assert json.loads(response.text)["code"] == "unknown_session"


def _rename_bot(config) -> None:
    config["agents"]["alicia"] = config["agents"].pop(BOT)


def _delete_bot(config) -> None:
    config["agents"].pop(BOT)


def _undeclare_store(config) -> None:
    config["memory_stores"].pop(BOT_STORE)


def _move_bot_to_global(config) -> None:
    config["agents"][BOT]["memory_store"] = "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        pytest.param(lambda env, room, seat, mp: _edit_config(env, mp, _rename_bot), id="renamed"),
        pytest.param(lambda env, room, seat, mp: _edit_config(env, mp, _delete_bot), id="deleted"),
        pytest.param(
            lambda env, room, seat, mp: _edit_config(env, mp, _undeclare_store),
            id="store-undeclared",
        ),
        pytest.param(
            lambda env, room, seat, mp: _edit_config(env, mp, _move_bot_to_global),
            id="moved-to-global",
        ),
        pytest.param(lambda env, room, seat, mp: room.remove_agent(seat.id), id="seat-removed"),
        pytest.param(
            lambda env, room, seat, mp: env.state.channel_manager.close(room.id),
            id="room-closed",
        ),
    ],
)
async def test_a_seat_without_its_bots_store_reaches_none(env, room, monkeypatch, event):
    """After the event, and after the next turn's re-bind, the seat's memory is refused."""
    seat = await _start(env, room, BOT)
    event(env, room, seat, monkeypatch)
    await _next_turn(seat)

    _assert_refused(await _learn(env, seat, ROOM_MARKER))
    assert _stores_holding(env, ROOM_MARKER) == []


@pytest.mark.asyncio
async def test_a_seat_follows_its_bot_to_a_reassigned_store(env, room, monkeypatch):
    """Refused while its record names the old store, then the new store from its next turn."""
    seat = await _start(env, room, BOT)
    _open_next_store(env)
    _edit_config(env, monkeypatch, _reassign)

    before_turn = await _learn(env, seat, "lesson before the next turn")
    await _next_turn(seat)
    after_turn = await _learn(env, seat, ROOM_MARKER)

    _assert_refused(before_turn)
    assert after_turn.status == 200, after_turn.text
    assert _stores_holding(env, ROOM_MARKER) == [NEXT_STORE]
    assert _stores_holding(env, "lesson before the next turn") == []


@pytest.mark.asyncio
async def test_after_a_restart_a_seat_binds_the_store_its_bot_has_then(env, room, monkeypatch):
    """A restarted gateway holds no live seat; the seat's loop binds the current store."""
    seat = await _start(env, room, BOT)
    env.state.sessions = _Sessions()
    _open_next_store(env)
    _edit_config(env, monkeypatch, _reassign)

    while_down = await _learn(env, seat, "lesson while the seat is down")
    await run_channel_agent(seat, room, env.state.sessions)
    restarted = await _learn(env, seat, ROOM_MARKER)

    _assert_refused(while_down)
    assert restarted.status == 200, restarted.text
    assert _stores_holding(env, ROOM_MARKER) == [NEXT_STORE]
    assert _stores_holding(env, "lesson while the seat is down") == []


@pytest.mark.asyncio
async def test_each_turn_runs_on_the_store_the_bot_has_when_it_starts(env, room, monkeypatch):
    """The real room loop re-binds before every turn, on a real session manager."""
    from test_cron_identity_gate import _kiro_provider

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
    from kiro_crew.session import SessionManager
    from kiro_crew.testing.wait import async_wait_until

    turn_stores: list[str] = []

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = _kiro_provider(
            session_key=session_key, agent=agent, channel_id=channel_id, **kwargs
        )

        async def _stream(_message):
            bound = await asyncio.to_thread(read_session_execution, session_key)
            turn_stores.append(bound.store.legacy_name)
            yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="noted")
            yield SimpleNamespace(kind=EVENT_COMPLETE)

        provider.stream = _stream
        return provider

    manager = SessionManager(KiroCrewConfig(), provider_factory=factory)
    seat = room.add_agent(role="Seat", agent_name=BOT, task="t")
    task = asyncio.create_task(run_channel_agent(seat, room, manager))
    try:
        await async_wait_until(lambda: seat.state == "listening", describe=lambda: seat.state)
        _open_next_store(env)
        _edit_config(env, monkeypatch, _reassign)
        await room.post("human", "note this")
        await async_wait_until(
            lambda: turn_stores and seat.state == "listening",
            describe=lambda: f"{seat.state} {turn_stores}",
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await manager.close_all()

    assert turn_stores == [NEXT_STORE]
