"""A Telegram conversation (a DM or a forum Topic) can run as a Bot.

``/agent`` offers the Bots (``config.agents`` keys) beside the templates, and a
Bot picked there runs the route AS that Bot: its kiro agent mode, workspace and
member identity reach ``get_or_create`` and the context builder gets the Bot's
template, the same collapse Slack's ``!bot`` uses (``slack/channel_crew.py``).
So each Topic of one forum can talk to its own Bot.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _hot_reload_helpers import prime_live
from telegram_helpers import FakeSessions, _dispatcher, _dm, _press

from kiro_crew.slack.channel_crew import ChannelCrew
from kiro_crew.telegram.dispatch import pickers, turn_engine

_ROUTE = ("direct", "7")
_BOT = "home-bot"
_CREW = ChannelCrew(kiro_agent="home-bot-mode", crew_agent=_BOT, cwd="/ws/member-home-bot")


class _KwSessions(FakeSessions):
    """Records every keyword ``get_or_create`` is given, crew fields included."""

    def __init__(self) -> None:
        super().__init__()
        self.alloc_kwargs: list[dict[str, Any]] = []

    async def get_or_create(self, key: str, **kw: Any) -> Any:  # type: ignore[override]
        self.alloc_kwargs.append(dict(kw))
        kw.pop("crew_agent", None)
        kw.pop("cwd", None)
        return await super().get_or_create(key, **kw)


def _bots(monkeypatch: pytest.MonkeyPatch, bots: dict[str, str]) -> None:
    monkeypatch.setattr("kiro_crew.slack.channel_crew.crew_bots", lambda: dict(bots))


# ── the picker ───────────────────────────────────────────────────────────────


def test_bot_rows_skip_the_default_bot_and_mark_each_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bots(monkeypatch, {"default": "", _BOT: "Home Bot", "harmony-dev": ""})

    assert pickers._bot_rows() == {_BOT: "🤖 Home Bot", "harmony-dev": "🤖 harmony-dev"}


def test_a_long_bot_name_is_cut_to_the_button_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _bots(monkeypatch, {_BOT: "x" * 200})

    (label,) = pickers._bot_rows().values()
    assert len(label) == pickers._AGENT_LABEL_MAX
    assert label.endswith("…")


def test_bots_come_first_and_a_shared_name_is_listed_once_as_the_bot() -> None:
    labels: dict[str, str] = {}
    names = pickers._picker_entries(["kirocrew-lite", _BOT], labels, {_BOT: "🤖 Home Bot"})

    assert names == [_BOT, "kirocrew-lite"]
    assert labels[_BOT] == "🤖 Home Bot"


def test_the_agent_picker_offers_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: ["kirocrew-lite"]))
    _bots(monkeypatch, {"default": "", _BOT: "Home Bot"})

    asyncio.run(d.handle_message(_dm("/agent")))

    _text, markup = client.sent[-1]
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        "• Default (kirocrew)",
        "🤖 Home Bot",
        "kirocrew-lite",
    ]


def test_a_failed_bot_lookup_still_shows_the_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: ["kirocrew-lite"]))

    def _boom() -> dict[str, str]:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(pickers, "_bot_rows", _boom)

    asyncio.run(d.handle_message(_dm("/agent")))

    _text, markup = client.sent[-1]
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        "• Default (kirocrew)",
        "kirocrew-lite",
    ]


def test_pressing_a_bot_sets_the_routes_agent_to_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: []))
    _bots(monkeypatch, {_BOT: "Home Bot"})
    asyncio.run(d.handle_message(_dm("/agent")))

    asyncio.run(d.on_callback(_press("g:1")))

    assert d._agent_picks.get(_ROUTE) == _BOT
    assert d._session_key(_ROUTE).split(":")[1] == _BOT
    assert "Home Bot" in client.edits[-1][1]


# ── the turn ─────────────────────────────────────────────────────────────────


def _bot_turn(
    monkeypatch: pytest.MonkeyPatch, crew: ChannelCrew | None, *, record: Any = None
) -> tuple[Any, _KwSessions, list[tuple[str, str]]]:
    prime_live("agent", default_agent="")
    d, _client, _sessions = _dispatcher({7})
    sess = _KwSessions()
    d.sessions = sess  # type: ignore[assignment]
    d._agent_picks.set(_ROUTE, _BOT)
    monkeypatch.setattr(turn_engine, "resolve_channel_crew", lambda name: crew)
    bound: list[tuple[str, str]] = []

    async def _bind(session_key: str, bot: str, sessions: Any, conversation_log: Any) -> None:
        bound.append((session_key, bot))

    monkeypatch.setattr(turn_engine, "bind_bot_memory", _bind)
    monkeypatch.setattr(turn_engine, "_has_execution_record", lambda key: record is not None)
    asyncio.run(d.handle_message(_dm("turn on the lights")))
    return d, sess, bound


def test_a_bot_turn_runs_the_bots_mode_workspace_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, sess, bound = _bot_turn(monkeypatch, _CREW)

    (alloc,) = sess.alloc_kwargs
    assert alloc["agent"] == "home-bot-mode"
    assert alloc["crew_agent"] == _BOT
    assert alloc["cwd"] == "/ws/member-home-bot"
    (build,) = d.ctx_builder.build_calls
    assert build["agent"] == "home-bot-mode"
    key = d._session_key(_ROUTE)
    assert bound == [(key, _BOT)]


def test_a_session_with_an_execution_record_is_not_bound_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _d, sess, bound = _bot_turn(monkeypatch, _CREW, record=object())

    assert bound == []
    assert sess.alloc_kwargs[0]["crew_agent"] == _BOT


def test_a_template_turn_passes_the_agent_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, sess, bound = _bot_turn(monkeypatch, None)

    (alloc,) = sess.alloc_kwargs
    assert alloc["agent"] == _BOT
    assert "crew_agent" not in alloc
    assert "cwd" not in alloc
    assert d.ctx_builder.build_calls[0]["agent"] == _BOT
    assert bound == []
