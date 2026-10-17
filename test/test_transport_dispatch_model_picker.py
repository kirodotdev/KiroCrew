"""The ``!model`` picker's rules, read through the ``DiscordDispatcher`` members that hold them.

``kiro_crew.discord.transport_dispatch_model_picker`` owns which models a session may be
offered, how long a posted button set stays pressable and how many are kept, and what a
pick promises the user. ``test_discord_interactions.py`` drives the picker end to end;
these pin the edges each rule decides on its own: what is offered when the backend
advertises nothing, fails, or advertises more than one message can carry; which pickers
a prune drops; and the outcome line for every state the session can be in when a model
is picked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.discord.transport_dispatch_model_picker import (
    _MODEL_PICKER_LIMIT,
    _MODEL_PICKER_MAX,
    _MODEL_PICKER_TTL_SECS,
    _ModelPicker,
)

AUTO = ("", "Auto (let the backend choose)")


class _Client:
    """A provider's ACP client: ``set_model`` records the id, or raises *error*."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.models: list[str] = []
        self.error = error

    async def set_model(self, model_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.models.append(model_id)


class _Sessions:
    """The session-manager calls the picker makes, each answer set by the test."""

    def __init__(self, provider: Any = None, *, live: bool = False, idle: bool = True) -> None:
        self.provider = provider
        self.live = live
        self.idle = idle
        self.released: list[str] = []

    def get_provider(self, key: str) -> Any:
        return self.provider

    def has_session(self, key: str) -> bool:
        return self.live

    async def try_acquire(self, key: str) -> bool:
        return self.idle

    def release(self, key: str) -> None:
        self.released.append(key)


def _host(sessions: _Sessions) -> Any:
    return SimpleNamespace(sessions=sessions, _model_pref={}, _model_pickers={})


def _advertising(entries: Any) -> Any:
    def available_models() -> Any:
        if isinstance(entries, BaseException):
            raise entries
        return entries

    return SimpleNamespace(available_models=available_models)


# ── what a session is offered ────────────────────────────────────────────────────


def test_a_session_with_no_provider_is_offered_only_auto() -> None:
    assert DiscordDispatcher._model_choices(_host(_Sessions()), "k") == (AUTO,)


def test_a_provider_that_advertises_nothing_callable_is_offered_only_auto() -> None:
    provider = SimpleNamespace(available_models=["not", "callable"])
    assert DiscordDispatcher._model_choices(_host(_Sessions(provider)), "k") == (AUTO,)


def test_an_advertisement_that_raises_is_offered_only_auto() -> None:
    provider = _advertising(RuntimeError("backend went away"))
    assert DiscordDispatcher._model_choices(_host(_Sessions(provider)), "k") == (AUTO,)


def test_auto_blank_and_malformed_entries_are_not_offered_twice_or_at_all() -> None:
    provider = _advertising(
        [
            {"modelId": "auto", "name": "Auto"},
            {"modelId": "  ", "name": "blank"},
            "not-a-dict",
            {"modelId": "m-1"},
            {"modelId": "m-2", "name": "Second"},
        ]
    )
    assert DiscordDispatcher._model_choices(_host(_Sessions(provider)), "k") == (
        AUTO,
        ("m-1", "m-1"),
        ("m-2", "Second"),
    )


def test_the_offer_stops_at_the_buttons_one_message_can_carry() -> None:
    provider = _advertising([{"modelId": f"m-{i}"} for i in range(_MODEL_PICKER_LIMIT + 6)])
    rows = DiscordDispatcher._model_choices(_host(_Sessions(provider)), "k")
    assert len(rows) == _MODEL_PICKER_LIMIT
    assert rows[0] == AUTO and rows[1] == ("m-0", "m-0")


# ── which pickers a prune keeps ──────────────────────────────────────────────────


def _picker(created_at: float) -> _ModelPicker:
    return _ModelPicker(
        scope_id="user:u1", channel_id="c1", message_id="m", created_at=created_at, choices=(AUTO,)
    )


def test_a_prune_drops_an_expired_picker_and_keeps_a_fresh_one() -> None:
    host = _host(_Sessions())
    now = 10_000.0
    host._model_pickers = {
        "c1:old": _picker(now - _MODEL_PICKER_TTL_SECS - 1),
        "c1:new": _picker(now - 1),
    }
    DiscordDispatcher._prune_model_pickers(host, now)
    assert list(host._model_pickers) == ["c1:new"]


def test_a_prune_evicts_the_oldest_pickers_past_the_retention_cap() -> None:
    host = _host(_Sessions())
    now = 10_000.0
    host._model_pickers = {f"c1:{i}": _picker(now - i) for i in range(_MODEL_PICKER_MAX + 2)}
    DiscordDispatcher._prune_model_pickers(host, now)
    assert len(host._model_pickers) == _MODEL_PICKER_MAX
    assert {f"c1:{_MODEL_PICKER_MAX}", f"c1:{_MODEL_PICKER_MAX + 1}"}.isdisjoint(
        host._model_pickers
    )


# ── what a pick promises ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_with_nothing_live_applies_to_the_next_message() -> None:
    host = _host(_Sessions(live=False))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "")
    assert outcome == "✅ Model set to Auto — it applies to your next message."
    assert host._model_pref == {"user:u1": ""}


@pytest.mark.asyncio
async def test_auto_on_a_live_session_waits_for_the_next_conversation() -> None:
    host = _host(_Sessions(live=True))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "")
    assert "this conversation keeps its current model" in outcome
    assert host.sessions.released == []


@pytest.mark.asyncio
async def test_a_model_with_nothing_live_applies_to_the_next_message() -> None:
    host = _host(_Sessions(live=False))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "m-1")
    assert outcome == "✅ Model set to m-1 — it applies to your next message."
    assert host._model_pref == {"user:u1": "m-1"}


@pytest.mark.asyncio
async def test_a_busy_session_keeps_its_model_and_is_not_released() -> None:
    host = _host(_Sessions(live=True, idle=False))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "m-1")
    assert "a reply is still running" in outcome
    assert host.sessions.released == []


@pytest.mark.asyncio
async def test_a_provider_that_cannot_switch_defers_to_the_next_conversation() -> None:
    host = _host(_Sessions(SimpleNamespace(client=None), live=True))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "m-1")
    assert "the switch applies to your next one" in outcome
    assert host.sessions.released == ["k"]


@pytest.mark.asyncio
async def test_a_failed_switch_says_so_keeps_the_preference_and_releases() -> None:
    client = _Client(error=RuntimeError("refused"))
    host = _host(_Sessions(SimpleNamespace(client=client), live=True))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "m-1")
    assert outcome.startswith("⚠️ Couldn't switch this conversation to m-1 (RuntimeError)")
    assert host._model_pref == {"user:u1": "m-1"}
    assert host.sessions.released == ["k"]


@pytest.mark.asyncio
async def test_a_live_switch_reaches_the_session_and_releases_it() -> None:
    client = _Client()
    host = _host(_Sessions(SimpleNamespace(client=client), live=True))
    outcome = await DiscordDispatcher._apply_model(host, "user:u1", "k", "m-1")
    assert outcome == "✅ Now using m-1."
    assert client.models == ["m-1"]
    assert host.sessions.released == ["k"]
