"""A default session environment for new dashboard chats."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import session_environment as se_runtime
from kiro_crew.config import session_environments as se


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


CODER = {"id": "dev", "name": "Dev", "template": "t", "workdir": "/w"}
OTHER = {"id": "big", "name": "Big", "template": "t2", "workdir": "/w"}


def _section(**extra):
    return {"coder_url": "https://c.example.com", "profiles": [CODER], **extra}


def test_the_default_must_name_a_profile_this_save_keeps():
    validated = se.validate_session_environments(_section(default_profile="dev"))
    assert validated["default_profile"] == "dev"
    ok = se.validate_session_environments(
        {**_section(default_profile="big"), "profiles": [CODER, OTHER]}
    )
    assert ok["default_profile"] == "big"
    # "" (and null) mean this machine.
    assert se.validate_session_environments(_section(default_profile=""))["default_profile"] == ""
    assert se.validate_session_environments(_section(default_profile=None))["default_profile"] == ""
    with pytest.raises(se.SessionEnvironmentsError, match="not one of the saved profiles"):
        se.validate_session_environments(_section(default_profile="gone"))
    with pytest.raises(se.SessionEnvironmentsError, match="profile id or empty"):
        se.validate_session_environments(_section(default_profile="--flag"))
    # A hand edit that breaks it loads as this machine rather than failing.
    assert se.load_default_profile({"default_profile": 7}) == ""
    assert se.load_default_profile({}) == ""


def _slot(messages=0, key="chat-1"):
    return SimpleNamespace(total_messages=messages, messages=[], key=key)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from kiro_crew.config import loader, paths
    from kiro_crew.dashboard.handlers import session_environments as handlers

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    state = {"default": "dev"}

    def load(cls):
        section = SimpleNamespace(
            coder_url="https://c.example.com",
            profiles=[SimpleNamespace(**CODER), SimpleNamespace(**OTHER)],
            default_profile=state["default"],
        )
        return SimpleNamespace(session_environments=section)

    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(load))
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: f"dashboard:{slot.key}")
    return handlers, state


def test_a_default_bind_retires_the_chats_local_shells(env, monkeypatch):
    """The chat is published before the default binds it, so another tab may have
    opened a local shell for it already: the bind retires that shell, and only it."""
    handlers, state = env
    from kiro_crew.dashboard.handlers import terminal

    killed: list[str] = []

    async def kill(sess):
        killed.append(sess.session_id)

    monkeypatch.setattr(terminal, "_kill_session", kill)

    def shell(sid, chat, remote=False):
        return SimpleNamespace(session_id=sid, chat_slot=chat, remote=remote, ws=None)

    registry = {
        "mine": shell("mine", "chat-1"),
        "other-chat": shell("other-chat", "chat-2"),
        "workspace": shell("workspace", "chat-1", remote=True),
    }
    assert asyncio.run(handlers.apply_default_environment(_slot(), terminals=registry)) is True
    assert killed == ["mine"]
    assert set(registry) == {"other-chat", "workspace"}
    # No default, no bind: nothing is retired.
    state["default"] = ""
    registry["again"] = shell("again", "chat-3")
    assert (
        asyncio.run(handlers.apply_default_environment(_slot(key="chat-3"), terminals=registry))
        is False
    )
    assert killed == ["mine"] and "again" in registry


class _Sessions:
    """The runtime registry, as far as the default bind reads it."""

    def __init__(self, registered=()):
        self.registered = set(registered)

    def has_session(self, key):
        return key in self.registered

    async def remove(self, key):
        self.registered.discard(key)


def test_a_default_bind_stops_a_local_prewarm_still_starting(env):
    """A prewarm in flight when the default binds would register a LOCAL runtime
    the first turn then reuses: the bind cancels it, so nothing runs here and the
    first turn resolves to the workspace."""
    handlers, _ = env
    key = "dashboard:chat-1"

    async def run():
        sessions = _Sessions()
        started, never = asyncio.Event(), asyncio.Event()

        async def prewarm():
            started.set()
            await never.wait()  # still in its handshake when the bind lands
            sessions.registered.add(key)

        slot = _slot()
        task = asyncio.create_task(prewarm())
        slot._eager_spawn_task = task
        slot._pending_reset_history_key = key
        await started.wait()
        bound = await handlers.apply_default_environment(slot, sessions)
        stopped_by_bind = task.cancelled()
        # Let a prewarm the bind left running finish its start and register.
        never.set()
        await asyncio.gather(task, return_exceptions=True)
        return bound, stopped_by_bind, slot, sessions

    bound, stopped_by_bind, slot, sessions = asyncio.run(run())
    assert bound is True
    assert stopped_by_bind
    assert sessions.registered == set(), "a local runtime registered after the bind"
    assert slot._pending_reset_history_key is None
    assert _choice(key) == "dev"
    assert se_runtime.session_runs_remotely(key) is True


def test_a_default_bind_removes_a_runtime_its_prewarm_registered(env):
    handlers, _ = env
    key = "dashboard:chat-1"

    async def run():
        sessions = _Sessions({key})

        async def finished():
            return None

        slot = _slot()
        slot._eager_spawn_task = asyncio.create_task(finished())
        await slot._eager_spawn_task
        return await handlers.apply_default_environment(slot, sessions), sessions

    bound, sessions = asyncio.run(run())
    assert bound is True and sessions.registered == set()
    assert _choice(key) == "dev"


def test_a_runtime_a_first_turn_registered_keeps_the_chat_local(env):
    handlers, _ = env
    key = "dashboard:chat-1"
    slot = _slot()
    slot._eager_spawn_task = None
    sessions = _Sessions({key})
    assert asyncio.run(handlers.apply_default_environment(slot, sessions)) is False
    assert sessions.registered == {key} and _choice(key) is None


def test_the_profile_list_follows_the_applied_snapshot_not_the_disk(env, monkeypatch):
    """Placement and the file browser read one config: the applied snapshot. A
    profile edit on disk the watcher has not applied is not advertised yet."""
    handlers, _ = env
    from kiro_crew.config import live

    applied = SimpleNamespace(
        session_environments=SimpleNamespace(
            coder_url="https://c.example.com",
            profiles=[SimpleNamespace(**{**CODER, "workdir": "/workspace/old"})],
            default_profile="",
        )
    )
    monkeypatch.setattr(live, "snapshot", lambda: applied)
    assert [p["workdir"] for p in handlers._profiles()] == ["/workspace/old"]
    # No snapshot yet (before the watcher starts): the disk is read.
    monkeypatch.setattr(live, "snapshot", lambda: None)
    assert [p["id"] for p in handlers._profiles()] == ["dev", "big"]


def test_the_profile_list_sends_each_template_for_the_picker(env):
    handlers, _ = env
    assert [(p["id"], p["template"]) for p in handlers._profiles()] == [("dev", "t"), ("big", "t2")]


def test_flag_off_offers_no_profile_and_binds_no_new_chat(env, monkeypatch):
    handlers, _ = env
    monkeypatch.delenv("KIROCREW_DEV_MODE")
    assert handlers._profiles() == []
    assert asyncio.run(handlers.apply_default_environment(_slot())) is False
    assert _choice("dashboard:chat-1") is None


def _choice(key):
    from kiro_crew.dashboard.handlers.session_environments import _choices

    return _choices().get(key)


def test_a_new_chat_is_bound_to_the_default(env):
    handlers, state = env
    assert asyncio.run(handlers.apply_default_environment(_slot())) is True
    assert _choice("dashboard:chat-1") == "dev"
    state["default"] = "big"
    assert asyncio.run(handlers.apply_default_environment(_slot(key="chat-2"))) is True
    assert _choice("dashboard:chat-2") == "big"


def test_the_default_never_moves_a_chat_that_has_a_choice_or_has_started(env):
    handlers, state = env
    from kiro_crew.dashboard.handlers.session_environments import _choices

    _choices().set("dashboard:chat-1", "big")
    assert asyncio.run(handlers.apply_default_environment(_slot())) is False
    assert _choice("dashboard:chat-1") == "big"
    assert asyncio.run(handlers.apply_default_environment(_slot(3, "chat-3"))) is False
    assert _choice("dashboard:chat-3") is None


def test_a_first_turn_that_registered_first_keeps_the_chat_local(env):
    """Another tab's first message started the session before the default
    landed: recording the default now would name a machine the chat is not on."""
    handlers, _state = env
    started = SimpleNamespace(has_session=lambda key: key == "dashboard:chat-1")
    assert asyncio.run(handlers.apply_default_environment(_slot(), started)) is False
    assert _choice("dashboard:chat-1") is None
    assert asyncio.run(handlers.apply_default_environment(_slot(key="chat-2"), started)) is True
    assert _choice("dashboard:chat-2") == "dev"


def test_the_default_waits_for_a_first_start_in_flight(env):
    """The bind takes the switch lock turn admission holds across a cold start."""
    from kiro_crew.llm_helpers_parts.model_fallback import slot_switch_session_lock
    from kiro_crew.testing.wait import until_parked

    handlers, _state = env

    async def run():
        start = slot_switch_session_lock("dashboard:chat-1")
        await start.acquire()
        try:
            bind = asyncio.ensure_future(handlers.apply_default_environment(_slot()))
            await until_parked(start)
            assert not bind.done()
        finally:
            start.release()
        return await bind

    assert asyncio.run(run()) is True
    assert _choice("dashboard:chat-1") == "dev"


def test_no_default_or_a_dangling_one_leaves_the_chat_on_this_machine(env):
    handlers, state = env
    for default in ("", "removed-profile"):
        state["default"] = default
        assert asyncio.run(handlers.apply_default_environment(_slot())) is False
        assert _choice("dashboard:chat-1") is None
