"""A default session environment for new dashboard chats."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.config import session_environments as se

CODER = {"id": "dev", "name": "Dev", "template": "t", "workdir": "/w"}
OTHER = {"id": "big", "name": "Big", "template": "t2", "workdir": "/w"}


def _section(**extra):
    return {"coder_url": "https://c.example.com", "profiles": [CODER], **extra}


def test_the_default_must_name_a_profile_this_save_keeps():
    assert (
        se.validate_session_environments(_section(default_profile="dev"))["default_profile"]
        == "dev"
    )
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


def _slot(messages=0):
    return SimpleNamespace(total_messages=messages, messages=[], key="chat-1")


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


def _choice(key):
    from kiro_crew.dashboard.handlers.session_environments import _choices

    return _choices().get(key)


def test_a_new_chat_is_bound_to_the_default(env):
    handlers, state = env
    assert asyncio.run(handlers.apply_default_environment(_slot())) is True
    assert _choice("dashboard:chat-1") == "dev"
    state["default"] = "big"
    slot = SimpleNamespace(total_messages=0, messages=[], key="chat-2")
    assert asyncio.run(handlers.apply_default_environment(slot)) is True
    assert _choice("dashboard:chat-2") == "big"


def test_the_default_never_moves_a_chat_that_has_a_choice_or_has_started(env):
    handlers, state = env
    from kiro_crew.dashboard.handlers.session_environments import _choices

    _choices().set("dashboard:chat-1", "big")
    assert asyncio.run(handlers.apply_default_environment(_slot())) is False
    assert _choice("dashboard:chat-1") == "big"
    started = SimpleNamespace(total_messages=3, messages=[], key="chat-3")
    assert asyncio.run(handlers.apply_default_environment(started)) is False
    assert _choice("dashboard:chat-3") is None


def test_no_default_or_a_dangling_one_leaves_the_chat_on_this_machine(env):
    handlers, state = env
    for default in ("", "removed-profile"):
        state["default"] = default
        assert asyncio.run(handlers.apply_default_environment(_slot())) is False
        assert _choice("dashboard:chat-1") is None
