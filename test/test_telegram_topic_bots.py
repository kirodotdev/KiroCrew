"""A Telegram conversation (a DM or a forum Topic) can run as a Bot.

``/bots`` offers the Bots (``config.agents`` keys), ``/agent`` keeps offering the
templates, and a Bot picked runs the route AS that Bot: its kiro agent mode, workspace and
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


def test_bot_rows_mark_each_bot_the_default_bot_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The built-in ``default`` Bot has its own row: the Default row resets to the
    # default template, which has none of the Bot's workspace or memory.
    _bots(monkeypatch, {"default": "", _BOT: "Home Bot", "harmony-dev": ""})

    assert pickers._bot_rows() == {
        "default": "🤖 default",
        _BOT: "🤖 Home Bot",
        "harmony-dev": "🤖 harmony-dev",
    }


def test_a_long_bot_name_is_cut_to_the_button_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _bots(monkeypatch, {_BOT: "x" * 200})

    (label,) = pickers._bot_rows().values()
    assert len(label) == pickers._AGENT_LABEL_MAX
    assert label.endswith("…")


def test_bots_lists_only_the_bots(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: ["kirocrew-lite"]))
    _bots(monkeypatch, {_BOT: "Home Bot"})

    asyncio.run(d.handle_message(_dm("/bots")))

    text, markup = client.sent[-1]
    assert text.startswith("Current Bot: ")
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        "• Default (kirocrew)",
        "🤖 Home Bot",
    ]


def test_agent_keeps_listing_only_the_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: ["kirocrew-lite"]))
    _bots(monkeypatch, {_BOT: "Home Bot"})

    asyncio.run(d.handle_message(_dm("/agent")))

    _text, markup = client.sent[-1]
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        "• Default (kirocrew)",
        "kirocrew-lite",
    ]


@pytest.mark.parametrize("command", ["/bots", "/bot"])
def test_bots_with_no_bots_says_where_to_make_one(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    _bots(monkeypatch, {})

    asyncio.run(d.handle_message(_dm(command)))

    assert "No Bots yet" in client.sent[-1][0]


def test_a_failed_bot_lookup_shows_no_bot_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})

    def _boom() -> dict[str, str]:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(pickers, "_bot_rows", _boom)

    asyncio.run(d.handle_message(_dm("/bots")))

    assert "No Bots yet" in client.sent[-1][0]


def test_bots_is_in_the_command_menu() -> None:
    from kiro_crew.telegram.commands import COMMAND_SPEC, parse_command

    assert "bots" in dict(COMMAND_SPEC)
    assert parse_command("/bots") == parse_command("/bot") == "bot"


def test_pressing_a_bot_sets_the_routes_agent_to_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    _bots(monkeypatch, {_BOT: "Home Bot"})
    asyncio.run(d.handle_message(_dm("/bots")))

    asyncio.run(d.on_callback(_press("g:1")))

    assert d._agent_picks.get(_ROUTE) == _BOT
    assert d._bot_picks.get(_ROUTE) == _BOT
    assert d._session_key(_ROUTE).split(":")[1] == _BOT
    assert client.edits[-1][1].startswith("✅ Bot set to 🤖 Home Bot")


# ── the turn ─────────────────────────────────────────────────────────────────


def _bot_turn(
    monkeypatch: pytest.MonkeyPatch,
    crew: ChannelCrew | None,
    *,
    record: Any = None,
    allowed: frozenset[int] = frozenset({7}),
    as_bot: bool = True,
    bound_store: str = "",
) -> tuple[Any, _KwSessions, list[tuple[str, str]]]:
    prime_live("agent", default_agent="")
    d, _client, _sessions = _dispatcher(set(allowed))
    sess = _KwSessions()
    d.sessions = sess  # type: ignore[assignment]
    d._agent_picks.set(_ROUTE, _BOT)
    if as_bot:
        d._bot_picks.set(_ROUTE, _BOT)
    monkeypatch.setattr(turn_engine, "resolve_channel_crew", lambda name: crew)
    from kiro_crew.telegram import transport_dispatch

    real_driver = transport_dispatch.TurnDriver
    d.driver_kwargs = []

    def _driver(*a: Any, **kw: Any) -> Any:
        d.driver_kwargs.append(kw)
        return real_driver(*a, **kw)

    monkeypatch.setattr(transport_dispatch, "TurnDriver", _driver)
    bound: list[tuple[str, str]] = []

    async def _bind(session_key: str, bot: str, sessions: Any, conversation_log: Any) -> None:
        bound.append((session_key, bot))

    monkeypatch.setattr(turn_engine, "bind_bot_memory", _bind)
    monkeypatch.setattr(turn_engine, "_has_execution_record", lambda key: record is not None)
    monkeypatch.setattr(turn_engine, "_bot_bound_store", lambda key: bound_store)
    d.audits = []
    monkeypatch.setattr(
        pickers, "_audit_bot_refusal", lambda uid, op, bot: d.audits.append((uid, op, bot))
    )
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
    # The Bot's template's hooks fire: its NAME has no spec file.
    assert d.driver_kwargs[0]["hook_agent"] == "home-bot-mode"
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


# ── owner-only ───────────────────────────────────────────────────────────────


def _said(client: Any) -> str:
    return "\n".join([str(m[0]) for m in client.sent] + [str(e[1]) for e in client.edits])


@pytest.mark.parametrize("uid", ["7", "8"])
def test_bots_is_refused_when_the_allow_list_names_no_single_owner(
    monkeypatch: pytest.MonkeyPatch, uid: str
) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7, 8})
    _bots(monkeypatch, {_BOT: "Home Bot"})

    asyncio.run(d.handle_message(_dm("/bots", uid=uid)))

    text, markup = client.sent[-1]
    assert text == pickers.BOT_OWNER_ONLY
    assert not markup
    assert d._agent_picks.get(("direct", uid)) in (None, "")


def test_a_bot_press_is_refused_once_the_owner_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    _bots(monkeypatch, {_BOT: "Home Bot"})
    asyncio.run(d.handle_message(_dm("/bots")))
    d._allowed.add(8)  # a second member joins the allow-list (hot reload)

    asyncio.run(d.on_callback(_press("g:1")))

    assert d._agent_picks.get(_ROUTE) in (None, "")
    assert client.edits[-1][1] == pickers.BOT_OWNER_ONLY


def test_a_template_press_is_not_owner_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    prime_live("agent", default_agent="")
    d, _client, _sessions = _dispatcher({7, 8})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: ["kirocrew-lite"]))
    asyncio.run(d.handle_message(_dm("/agent")))

    asyncio.run(d.on_callback(_press("g:1")))

    assert d._agent_picks.get(_ROUTE) == "kirocrew-lite"


def test_a_bot_turn_from_a_shared_allow_list_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, sess, bound = _bot_turn(monkeypatch, _CREW, allowed=frozenset({7, 8}))

    assert sess.alloc_kwargs == []
    assert bound == []
    assert pickers.BOT_OWNER_ONLY in _said(d.client)
    # Audited like the command and the press refusals.
    assert d.audits == [(7, "telegram.bot_turn", _BOT)]


def test_the_owner_rule() -> None:
    assert pickers.is_bot_owner({7}, 7)
    assert not pickers.is_bot_owner({7}, 8)
    assert not pickers.is_bot_owner({7, 8}, 7)
    assert not pickers.is_bot_owner(set(), 7)


# ── a template that shares a Bot's name ─────────────────────────────────────


def test_an_agent_pick_of_a_name_that_is_also_a_bot_runs_the_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Installing a template gives it a ``config.agents`` record of the same name,
    # so only a ``/bots`` pick may run the name as a Bot.
    d, sess, bound = _bot_turn(monkeypatch, _CREW, as_bot=False)

    (alloc,) = sess.alloc_kwargs
    assert alloc == {**alloc, "agent": _BOT}
    assert "crew_agent" not in alloc
    assert bound == []


def test_switching_kind_under_one_name_starts_a_fresh_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: [_BOT]))
    _bots(monkeypatch, {_BOT: "Home Bot"})
    asyncio.run(d.handle_message(_dm("/bots")))
    asyncio.run(d.on_callback(_press("g:1", message_id=101)))
    bot_key = d._session_key(_ROUTE)

    asyncio.run(d.handle_message(_dm("/agent")))
    _text, markup = client.sent[-1]
    # The template row of the same name is not marked as the current pick.
    assert [row[0]["text"] for row in markup["inline_keyboard"]][1] == _BOT
    asyncio.run(d.on_callback(_press("g:1", message_id=102)))

    assert d._agent_picks.get(_ROUTE) == _BOT
    assert d._bot_picks.get(_ROUTE) in (None, "")
    assert d._session_key(_ROUTE) != bot_key
    assert client.edits[-1][1] == f"✅ Agent set to {_BOT}."


def test_the_busy_reply_names_the_command_that_was_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prime_live("agent", default_agent="")
    d, client, sessions = _dispatcher({7})
    _bots(monkeypatch, {_BOT: "Home Bot"})
    monkeypatch.setattr(sessions, "is_busy", lambda key: True, raising=False)

    out = asyncio.run(pickers._apply_pick(d, _ROUTE, _BOT, "🤖 Home Bot", bot=True))

    assert "send /bots again" in out


# ── a key a Bot bound, reached without a Bot pick ───────────────────────────


@pytest.mark.parametrize(
    "allowed",
    [
        # The owner after a restart: the picks are gone, ``/agent`` names the Bot.
        frozenset({7}),
        # Another allow-listed member's ``/agent`` pick of the Bot's name.
        frozenset({7, 8}),
    ],
)
def test_a_template_turn_on_a_key_a_bot_bound_is_refused(
    monkeypatch: pytest.MonkeyPatch, allowed: frozenset[int]
) -> None:
    # The template turn's key holds Bot ``home-bot``'s execution record (the
    # generation is seeded by name), so it would read the Bot's private store.
    d, sess, bound = _bot_turn(
        monkeypatch, _CREW, as_bot=False, allowed=allowed, bound_store="home-bot"
    )

    assert sess.alloc_kwargs == []
    assert bound == []
    assert pickers.BOT_BOUND_REFUSAL in _said(d.client)
    assert d.audits == [(7, "telegram.bot_bound_turn", "home-bot")]


def test_a_template_turn_on_a_global_key_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _d, sess, _bound = _bot_turn(monkeypatch, None, as_bot=False, bound_store="")

    assert len(sess.alloc_kwargs) == 1


def test_a_bot_named_like_the_default_template_is_a_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No explicit pick: the route runs the default template ``kirocrew``. Its
    # Bot of the same name must switch, not answer "Already using".
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    _bots(monkeypatch, {"kirocrew": "Crew"})
    before = d._session_key(_ROUTE)
    asyncio.run(d.handle_message(_dm("/bots")))

    asyncio.run(d.on_callback(_press("g:1")))

    assert client.edits[-1][1] == "✅ Bot set to 🤖 Crew."
    assert d._agent_picks.get(_ROUTE) == "kirocrew"
    assert d._bot_picks.get(_ROUTE) == "kirocrew"
    assert d._session_key(_ROUTE) != before
