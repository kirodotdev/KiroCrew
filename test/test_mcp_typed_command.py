"""A ``/mcp add|remove`` the person typed becomes an ``mcp.server`` card, on every harness.

The parser reads kiro-cli's own flag spelling; the origin check decides before any
harness is looked up; the answer raises the card through the same proposal body the
``request_server`` route uses, or replies with the MCP Servers page link.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from turn_harness import TurnScript, run_turn

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import mcp_typed_command as mtc


def test_add_reads_kiro_cli_flags_into_an_entry() -> None:
    req = mtc.parse_typed_mcp(
        '/mcp add --name weather --command npx --args "-y,@acme/weather-mcp" '
        "--env CITY=Paris --env UNITS=metric"
    )
    assert (req.action, req.server, req.target) == ("add", "weather", "crew")
    assert req.entry == {
        "command": "npx",
        "args": ["-y", "@acme/weather-mcp"],
        "env": {"CITY": "Paris", "UNITS": "metric"},
    }


def test_args_take_the_json_form_and_an_escaped_comma() -> None:
    req = mtc.parse_typed_mcp("""/mcp add --name a --command x --args '["p,q", "r"]'""")
    assert req.entry["args"] == ["p,q", "r"]
    req = mtc.parse_typed_mcp(r"/mcp add --name a --command x --args 'p\,q,r'")
    assert req.entry["args"] == ["p,q", "r"]


def test_global_scope_targets_kiro_clis_file_and_url_servers_work() -> None:
    req = mtc.parse_typed_mcp("/mcp add --name=docs --url=https://example.com/mcp --scope global")
    assert (req.entry, req.target) == ({"url": "https://example.com/mcp"}, "kiro-cli")
    assert req.card_params()["target"] == "kiro-cli"


def test_remove_takes_only_the_name() -> None:
    req = mtc.parse_typed_mcp("/mcp remove --name weather")
    assert req.card_params() == {"action": "remove", "server": "weather", "use_now": True}


@pytest.mark.parametrize(
    "message",
    [
        "/mcp add --command npx",  # no name
        "/mcp add --name a",  # no command or url
        "/mcp add --name a --command x --url https://e.com",  # both
        "/mcp add --name a --command x --scope workspace",  # a project file: not this card
        "/mcp add --name a --command x --agent kirocrew",  # an agent spec
        "/mcp add --name a --command x --bogus 1",
        "/mcp add --name a --command x --disabled",
        "/mcp add --name a --command x --env NOEQUALS",
        "/mcp add --name 'a --command x",  # unbalanced quote
        "/mcp remove --name a --command x",
        "/mcp remove --name a --scope global",  # the card removes from every file
        "/mcp remove --name a --scope default",
    ],
)
def test_a_command_the_card_cannot_carry_is_unreadable(message: str) -> None:
    with pytest.raises(mtc.TypedMcpUnreadable):
        mtc.parse_typed_mcp(message)


def _turn(**fields: Any) -> SimpleNamespace:
    base = {
        "_directive_user_origin": True,
        "_directive_channel_origin": False,
        "_directive_self_wake": False,
        "_turn_actor": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


ADD = "/mcp add --name weather --command npx"


def test_only_the_persons_own_words_are_intercepted() -> None:
    assert mtc.typed_mcp_origin(_turn(), ADD, synthetic=False) == "dashboard"
    assert (
        mtc.typed_mcp_origin(
            _turn(_directive_user_origin=False, _directive_channel_origin=True),
            ADD,
            synthetic=False,
        )
        == "channel"
    )
    assert mtc.typed_mcp_origin(_turn(), ADD, synthetic=True) is None
    assert mtc.typed_mcp_origin(_turn(_directive_self_wake=True), ADD, synthetic=False) is None
    assert mtc.typed_mcp_origin(_turn(_turn_actor="gateway"), ADD, synthetic=False) is None
    assert mtc.typed_mcp_origin(_turn(_directive_user_origin=False), ADD, synthetic=False) is None
    # Every other /mcp keeps its own path.
    assert mtc.typed_mcp_origin(_turn(), "/mcp", synthetic=False) is None
    assert mtc.typed_mcp_origin(_turn(), "/mcp auth github", synthetic=False) is None


@pytest.mark.asyncio
async def test_a_typed_command_is_answered_before_any_session_is_claimed() -> None:
    # Harness-independent because no session is ever asked for: the answer comes
    # from Crew before the turn reaches KAS's local answers or kiro-cli's forward.
    record = await run_turn(
        TurnScript(
            message="/mcp add --name a --bogus 1",
            run_kwargs={"_directive_user_origin": True},
        )
    )
    replies = [row["content"] for row in record.window if row["role"] != "user"]
    assert mtc.PAGE_REPLY in replies
    assert not [c for c in record.session_calls if c.name == "get_or_create"]
    assert record.provider_calls == ()


@pytest.mark.asyncio
async def test_a_channel_turn_tells_its_linked_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # The turn ends before the reply mirror, so the gate posts the page reply
    # into the linked thread itself.
    posted: list[str] = []

    async def deliver(_state, _slot, _sessions, _key, text: str) -> None:
        posted.append(text)

    monkeypatch.setattr(chat_runner, "_deliver_linked_slack_message", deliver)
    record = await run_turn(
        TurnScript(
            message=ADD,
            run_kwargs={"_directive_user_origin": True, "_directive_channel_origin": True},
        )
    )
    assert posted == [mtc.CHANNEL_REPLY]
    assert record.provider_calls == ()


class _State:
    def __init__(self) -> None:
        self.pushed = 0

    def push_slots_update(self) -> None:
        self.pushed += 1


def _answer(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    *,
    channel: bool = False,
    fail=None,
    sent: list[str] | None = None,
):
    from kiro_crew.dashboard.handlers import change_cards_propose

    proposed: list[dict[str, Any]] = []
    replies: list[str] = []

    async def propose(state, slot_key, sk, *, kind, params, reason, **_kw):
        if fail is not None:
            raise fail
        proposed.append({"slot": slot_key, "sk": sk, "kind": kind, "params": params})
        return {"id": "c1"}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    slot = SimpleNamespace(key="slot-1", agent="kirocrew")
    outcome = asyncio.run(
        mtc.answer_typed_mcp(
            _State(),
            slot,
            "chat-1",
            message,
            channel=channel,
            reply_with=lambda _slot, text: replies.append(text),
            reply_to_channel=None if sent is None else _collect(sent),
        )
    )
    return outcome, proposed, replies


def _collect(sent: list[str]):
    async def send(text: str) -> None:
        sent.append(text)

    return send


def test_a_typed_add_raises_the_prefilled_card(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, proposed, replies = _answer(monkeypatch, ADD + " --args -y")
    assert outcome == "card" and replies == []
    assert proposed == [
        {
            "slot": "slot-1",
            "sk": "chat-1",
            "kind": "mcp.server",
            "params": {
                "action": "add",
                "server": "weather",
                "use_now": True,
                "entry": {"command": "npx", "args": ["-y"]},
                "uses_chat_folder": True,
            },
        }
    ]


@pytest.mark.parametrize(
    "message",
    [
        "/mcp add --name fs --command fs-mcp --args .",
        # A git tool named only by its package: the folder detector cannot see it.
        "/mcp add --name git --command uvx --args mcp-server-git",
    ],
)
def test_a_typed_add_always_stays_per_chat(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    _o, proposed, _r = _answer(monkeypatch, message)
    assert proposed[0]["params"]["uses_chat_folder"] is True


def test_a_typed_remove_raises_the_remove_card(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, proposed, _r = _answer(monkeypatch, "/mcp remove --name weather")
    assert outcome == "card"
    assert proposed[0]["params"] == {"action": "remove", "server": "weather", "use_now": True}


def test_an_unreadable_command_replies_with_the_page_link(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, proposed, replies = _answer(monkeypatch, "/mcp add --name a --bogus 1")
    assert (outcome, proposed) == ("page", [])
    assert replies == [mtc.PAGE_REPLY]
    assert "[MCP Servers](/mcp)" in replies[0]


def test_a_channel_turn_replies_with_the_page_link(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    outcome, proposed, replies = _answer(monkeypatch, ADD, channel=True, sent=sent)
    assert (outcome, proposed, replies) == ("page", [], [mtc.PAGE_REPLY])
    assert sent == [mtc.CHANNEL_REPLY]


def test_a_dashboard_turn_posts_nothing_to_a_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    _answer(monkeypatch, "/mcp add --name a --bogus 1", sent=sent)
    assert sent == []


def test_a_refused_card_names_why_and_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.change_card_catalog import CardCatalogError

    err = CardCatalogError("already_exists", "an MCP server 'weather' already exists")
    outcome, _p, replies = _answer(monkeypatch, ADD, fail=err)
    assert outcome == "refused"
    assert replies == ["An MCP server 'weather' already exists. " + mtc.PAGE_REPLY]
