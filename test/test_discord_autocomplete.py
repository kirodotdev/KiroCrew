"""Discord slash-command type-ahead (interaction type 4) for /agent, /sessions, /bot, /model.

The invariant: an autocomplete request gets exactly ONE answer, callback type 8, and
nothing else (no deferral, no message, no channel write). Every refusal is an empty
choice list: a denied user, a channel that is not an approved thread, a governance
deny, a slow lookup, and ``/sessions`` or ``/bot`` anywhere but the owner's DM.

Driven through ``on_interaction`` with a real ``DiscordClient`` whose REST layer is
recorded, so what is asserted is the request Discord would receive.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import discord_sessions_helpers
import pytest
from discord_helpers import _dispatcher

from kiro_crew.discord import transport_dispatch as td
from kiro_crew.discord import transport_dispatch_interactions as tdi
from kiro_crew.discord.client import DISCORD_OK, DiscordApiResult, DiscordClient
from kiro_crew.discord.client_gateway import _HANDLED_INTERACTIONS, DiscordInteraction
from kiro_crew.messaging.session_resume import SessionChoice

_APP = "777000111"
_THREAD = "5550001"
_DM = "5550002"
_OTHER_CHANNEL = "5550003"

_isolated_home = discord_sessions_helpers._isolated_home


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, str]] = []
        #: The caller check each call handed the REST ladder for its waits.
        self.caller_checks: list[Any] = []

    async def api_json(
        self,
        method: str,
        path: str,
        payload: Any,
        *,
        destination: str = "",
        caller_permitted: Any = None,
        **_kw: Any,
    ) -> DiscordApiResult:
        self.calls.append((method, path, payload, destination))
        self.caller_checks.append(caller_permitted)
        return DiscordApiResult(DISCORD_OK, data={}, status=204)

    def only_answer(self) -> tuple[list[dict[str, str]], str]:
        """The single autocomplete callback's choices and destination."""
        assert len(self.calls) == 1, self.calls
        method, path, body, destination = self.calls[0]
        assert (method, path) == ("POST", "/interactions/i4/actok/callback")
        assert set(body) == {"type", "data"}
        assert body["type"] == 8
        assert set(body["data"]) == {"choices"}
        return body["data"]["choices"], destination


def _wired(allowed: set[str] | None = None) -> tuple[Any, Any, _Recorder]:
    d, _fake, sess = _dispatcher(allowed or {"u1"}, allowed_threads={_THREAD})
    client = DiscordClient(token="bot-secret")
    client.application_id = _APP
    client._channel_types[_THREAD] = 11  # a public thread, so no lookup is needed
    client._channel_types[_OTHER_CHANNEL] = 0  # an ordinary guild text channel
    recorder = _Recorder()
    client.api_json = recorder.api_json  # type: ignore[method-assign]
    d.client = client
    return d, sess, recorder


def _typing(command: str, channel: str, text: str = "", *, user: str = "u1") -> DiscordInteraction:
    option = "query" if command == "sessions" else "name"
    return DiscordInteraction(
        interaction_id="i4",
        interaction_token="actok",
        channel_id=channel,
        user_id=user,
        message_id="",
        guild_id="" if channel == _DM else "g1",
        kind=4,
        command_name=command,
        options={option: text},
        focused_option=option,
    )


@pytest.fixture
def governance(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Both channels ceilings, permitting unless a test flips one."""
    state = {"inbound": True, "outbound": True, "calls": 0}

    async def _inbound(_channel: str) -> bool:
        state["calls"] += 1
        return state["inbound"]

    async def _outbound(_channel: str) -> bool:
        state["calls"] += 1
        return state["outbound"]

    monkeypatch.setattr(td, "channel_inbound_permitted", _inbound)
    monkeypatch.setattr(td, "channel_outbound_permitted", _outbound)
    return state


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    names = ["coder", "reviewer", "researcher"]
    monkeypatch.setattr(tdi, "pickable_agent_names", lambda: list(names))
    return names


def _names(choices: list[dict[str, str]]) -> list[str]:
    return [c["name"] for c in choices]


# ── the gateway forwards it ──


@pytest.mark.asyncio
async def test_gateway_normalizes_an_autocomplete_request() -> None:
    assert 4 in _HANDLED_INTERACTIONS
    client = DiscordClient(token="bot-secret")
    seen: list[DiscordInteraction] = []

    async def _handler(itx: DiscordInteraction) -> None:
        seen.append(itx)

    client._on_interaction = _handler
    client._on_dispatch(
        "INTERACTION_CREATE",
        {
            "id": "i4",
            "token": "actok",
            "type": 4,
            "channel_id": _DM,
            "user": {"id": "u1", "username": "kyle"},
            "data": {
                "name": "agent",
                "options": [{"type": 3, "name": "name", "value": "rev", "focused": True}],
            },
        },
    )
    await asyncio.gather(*client._handler_tasks)
    (itx,) = seen
    assert itx.is_autocomplete and not itx.is_command
    assert (itx.command_name, itx.focused_option, itx.options) == ("agent", "name", {"name": "rev"})


# ── /agent: matching, the 25 cap, and the only answer shape ──


@pytest.mark.asyncio
async def test_empty_query_offers_default_and_every_agent(governance, agents) -> None:
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM))
    choices, destination = rec.only_answer()
    assert choices == [{"name": n, "value": n} for n in ["default", *agents]]
    assert destination == _DM


@pytest.mark.asyncio
async def test_query_matches_case_insensitively(governance, agents) -> None:
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM, "  RE"))
    choices, _dest = rec.only_answer()
    assert _names(choices) == ["reviewer", "researcher"]


@pytest.mark.asyncio
async def test_no_match_is_an_empty_answer(governance, agents) -> None:
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM, "zzz"))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
async def test_more_than_25_matches_are_cut_to_25(governance, monkeypatch) -> None:
    many = [f"agent-{i:02d}" for i in range(40)]
    monkeypatch.setattr(tdi, "pickable_agent_names", lambda: list(many))
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM, "agent-"))
    choices, _dest = rec.only_answer()
    assert _names(choices) == many[:25]


@pytest.mark.asyncio
async def test_approved_thread_gets_choices(governance, agents) -> None:
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _THREAD, "cod"))
    assert rec.only_answer() == ([{"name": "coder", "value": "coder"}], _THREAD)


# ── refusals are empty answers, never a message ──


@pytest.mark.asyncio
async def test_denied_user_gets_empty_choices_without_a_governance_read(governance, agents) -> None:
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM, user="intruder"))
    assert rec.only_answer() == ([], "")
    assert governance["calls"] == 0


@pytest.mark.asyncio
async def test_channel_that_is_not_an_approved_thread_gets_empty_choices(
    governance, agents
) -> None:
    # The command path answers here with a refusal MESSAGE; type-ahead must not.
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _OTHER_CHANNEL))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", ["inbound", "outbound"])
async def test_governance_deny_gets_empty_choices(governance, agents, ceiling) -> None:
    governance[ceiling] = False
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
async def test_roster_withdrawn_during_the_lookup_empties_the_answer(governance, monkeypatch):
    d, _sess, rec = _wired()

    def _names_then_withdraw() -> list[str]:
        d._allowed = set()
        return ["coder"]

    monkeypatch.setattr(tdi, "pickable_agent_names", _names_then_withdraw)
    await d.on_interaction(_typing("agent", _DM))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
async def test_a_lookup_past_the_budget_answers_empty(governance, agents, monkeypatch) -> None:
    never = asyncio.Event()

    async def _never_answers(_channel: str) -> bool:
        await never.wait()
        return True

    monkeypatch.setattr(td, "channel_inbound_permitted", _never_answers)
    # flake-ok: the read never finishes, so a zero budget times out with no race
    monkeypatch.setattr(tdi, "_AUTOCOMPLETE_BUDGET_SECS", 0)
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
async def test_a_failed_governance_read_answers_empty(governance, agents, monkeypatch) -> None:
    async def _broken(_channel: str) -> bool:
        raise OSError("profile store unreadable")

    monkeypatch.setattr(td, "channel_inbound_permitted", _broken)
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
async def test_a_failed_lookup_answers_empty(governance, monkeypatch) -> None:
    def _boom() -> list[str]:
        raise OSError("spec dir unreadable")

    monkeypatch.setattr(tdi, "pickable_agent_names", _boom)
    d, _sess, rec = _wired()
    await d.on_interaction(_typing("agent", _DM))
    assert rec.only_answer() == ([], "")


# ── /sessions and /bot: the owner's DM only ──


class _Suggestions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, command, user_id, query, *, native_key, limit):
        self.calls.append((command, user_id, query))
        assert limit == 25
        return (SessionChoice(key="dashboard:chat-1", title="Release notes"),)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["sessions", "bot"])
async def test_private_titles_are_offered_in_the_owners_dm(governance, command) -> None:
    d, _sess, rec = _wired()
    suggest = _Suggestions()
    d._session_resume.suggestions = suggest
    await d.on_interaction(_typing(command, _DM, "rel"))
    choice = {"name": "Release notes", "value": "session-key:dashboard:chat-1"}
    assert rec.only_answer() == ([choice], _DM)
    assert suggest.calls == [(command, "u1", "rel")]
    # A REST ladder wait re-reads the owner rule: a second allowed user (so no
    # owner any more) stops the titles from going out after the wait.
    (still_owner,) = rec.caller_checks
    assert still_owner() is True
    d._session_resume.reconfigure({"u1", "u2"})
    assert still_owner() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["sessions", "bot"])
async def test_private_titles_are_never_offered_in_a_thread(governance, command) -> None:
    d, _sess, rec = _wired()
    suggest = _Suggestions()
    d._session_resume.suggestions = suggest
    await d.on_interaction(_typing(command, _THREAD, "rel"))
    assert rec.only_answer() == ([], "")
    assert suggest.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["sessions", "bot"])
async def test_private_titles_need_an_owner(governance, command) -> None:
    # Two allowed users means no owner, so neither gets the owner's titles.
    d, _sess, rec = _wired({"u1", "u2"})
    suggest = _Suggestions()
    d._session_resume.suggestions = suggest
    await d.on_interaction(_typing(command, _DM, "rel"))
    assert rec.only_answer() == ([], "")
    assert suggest.calls == []


@pytest.mark.asyncio
async def test_session_suggestions_reuse_the_pickers_eligibility() -> None:
    d, _sess, _rec = _wired()

    class _Log:
        def list_sessions_indexed(self) -> list[dict[str, Any]]:
            return [
                {"key": "dashboard:chat-1", "title": "Release notes"},
                {"key": "dashboard:chat-2", "title": "Secret", "memory_mode": "incognito"},
            ]

    resume = d._session_resume
    resume.conv_log = _Log()
    resume._controller.conv_log = resume.conv_log
    found = await resume.suggestions("sessions", "u1", "", native_key="", limit=25)
    assert [c.title for c in found] == ["Release notes"]
    assert await resume.suggestions("sessions", "u2", "", native_key="", limit=25) == ()


# ── /model ──


@pytest.mark.asyncio
async def test_model_offers_the_advertised_list_with_auto(governance) -> None:
    d, sess, rec = _wired()
    sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}, {"modelId": "m-deep", "name": "Deep"}]
    await d.on_interaction(_typing("model", _THREAD))
    choices, destination = rec.only_answer()
    assert choices == [
        {"name": "Auto (let the backend choose)", "value": "auto"},
        {"name": "Fast", "value": "m-fast"},
        {"name": "Deep", "value": "m-deep"},
    ]
    assert destination == _THREAD


@pytest.mark.asyncio
async def test_model_with_nothing_advertised_offers_nothing(governance) -> None:
    d, sess, rec = _wired()
    sess._gp.models = []
    await d.on_interaction(_typing("model", _DM))
    assert rec.only_answer() == ([], "")


@pytest.mark.asyncio
async def test_a_picked_model_is_applied_and_a_guess_shows_the_list(governance) -> None:
    d, sess, _rec = _wired()
    sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
    said: list[str] = []
    applied: list[str] = []

    async def _say(text: str, *, components: Any = None) -> str:
        said.append(text)
        return "m1"

    async def _apply(_scope: str, _key: str, model_id: str) -> str:
        applied.append(model_id)
        return f"applied {model_id or 'auto'}"

    d._apply_model = _apply
    await d._handle_model(_DM, "user:u1", "k", "m-fast", reply=_say, user_id="u1")
    await d._handle_model(_DM, "user:u1", "k", "AUTO", reply=_say, user_id="u1")
    assert applied == ["m-fast", ""]
    assert said == ["applied m-fast", "applied auto"]
    await d._handle_model(_DM, "user:u1", "k", "m-fas", reply=_say, user_id="u1")
    assert applied == ["m-fast", ""]
    assert said[-1].startswith("That is not a model this conversation offers")


def test_overlong_values_are_dropped_and_names_cut() -> None:
    rows = [("n" * 150, "ok"), ("fine", "v" * 101)]
    assert tdi._autocomplete_choices(rows, "") == [{"name": "n" * 100, "value": "ok"}]


# ── a picked /sessions or /bot suggestion names its session by key ──

_LONG = "Quarterly planning notes for the platform migration and the on-call rotation review"


@pytest.mark.asyncio
async def test_a_picked_bot_suggestion_binds_even_when_its_title_was_shortened(tmp_path) -> None:
    from chat_test_helpers import _make_state
    from discord_sessions_helpers import _ConversationLog
    from discord_sessions_helpers import _dispatcher as _sessions_dispatcher
    from discord_sessions_helpers import _message

    state = _make_state(tmp_path)
    bot = state.get_or_create_slot("member-radar", mode="member")
    bot.title, bot._titled, bot.agent = _LONG, True, "radar"
    log = _ConversationLog([], {"dashboard:member-radar": [{"role": "assistant", "content": "x"}]})
    d, _client, _sess = _sessions_dispatcher({"u1"}, log)
    d._session_resume.dashboard_state = SimpleNamespace(
        _slots=state._slots, push_slots_update=lambda: None
    )
    (found,) = await d._session_resume.suggestions("bot", "u1", "", native_key="", limit=25)
    assert found.title != _LONG  # shortened for display
    await d.handle_message(_message(f"!bot session-key:{found.key}"))
    assert d._session_resume.resumed_session("c1") == found.key


@pytest.mark.asyncio
async def test_a_picked_session_suggestion_offers_that_session_alone(tmp_path) -> None:
    from discord_sessions_helpers import _ConversationLog
    from discord_sessions_helpers import _dispatcher as _sessions_dispatcher
    from discord_sessions_helpers import _message

    rows = [
        {"key": "dashboard_chat-1", "title": _LONG, "memory_mode": "persistent"},
        {"key": "dashboard_chat-2", "title": "Other", "memory_mode": "persistent"},
    ]
    log = _ConversationLog(rows, {"dashboard:chat-1": [], "dashboard:chat-2": []})
    d, client, _sess = _sessions_dispatcher({"u1"}, log)
    found = await d._session_resume.suggestions("sessions", "u1", "", native_key="", limit=25)
    first = next(c for c in found if c.title != "Other")
    await d.handle_message(_message(f"!sessions session-key:{first.key}"))
    _text, components = client.sent[-1]
    labels = [b["label"] for row in components or [] for b in row["components"]]
    assert len(labels) == 1 and labels[0].startswith("1. Quarterly planning")
    # A key the picker would not list offers nothing.
    await d.handle_message(_message("!sessions session-key:dashboard:chat-404"))
    text, components = client.sent[-1]
    assert not components
    assert "session-key" not in text  # the key is internal, never echoed


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawn", ["user", "thread", "ceiling"])
async def test_a_typed_model_is_not_applied_once_admission_is_withdrawn(
    governance, withdrawn
) -> None:
    # Admission ran before routing awaited; a revocation in that window must
    # stop the switch, not only the confirmation.
    d, sess, _rec = _wired()
    sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
    said: list[str] = []
    applied: list[str] = []

    async def _say(text: str, *, components: Any = None) -> str:
        said.append(text)
        return "m1"

    async def _apply(_scope: str, _key: str, model_id: str) -> str:
        applied.append(model_id)
        return "applied"

    d._apply_model = _apply
    if withdrawn == "user":
        d._allowed = set()
    elif withdrawn == "thread":
        d._allowed_threads.discard(_THREAD)
    else:
        governance["inbound"] = False
    await d._handle_model(_THREAD, f"thread:{_THREAD}", "k", "m-fast", reply=_say, user_id="u1")
    assert applied == [] and said == []
    assert d._model_pref.get(f"thread:{_THREAD}") is None


@pytest.mark.asyncio
async def test_a_dm_type_ahead_records_the_pairing_only_for_an_allowed_caller(
    governance, agents
) -> None:
    # The answer's rate-limit re-check needs the DM's peer, so an allowed
    # caller's first type-ahead after a restart records it; a denied one never.
    d, _sess, _rec = _wired()
    await d.on_interaction(_typing("agent", _DM, user="intruder"))
    assert d.client.cached_dm_recipient(_DM) is None
    await d.on_interaction(_typing("agent", _DM))
    assert d.client.cached_dm_recipient(_DM) == "u1"
