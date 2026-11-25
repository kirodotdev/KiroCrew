"""Discord ``[OPTIONS:]`` string selects: "Choose several..." and lists over 20.

Up to 20 choices show as buttons with a multi-select last; a longer list shows as
string selects of 25. A select press is gated like a button press, answers only
with choices the menu offered, answers once, and an older prompt answers nothing.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient, _dispatcher

import kiro_crew.discord.transport_dispatch as td_mod
from kiro_crew.discord import renderer_components as rc
from kiro_crew.discord.client import DiscordInteraction
from kiro_crew.discord.renderer import DiscordRenderer, session_provenance_tag
from kiro_crew.discord.transport import DISCORD_CAPABILITIES


@pytest.fixture(autouse=True)
def _fresh_select_registry(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(rc, "_OPTION_SELECTS", OrderedDict())


def _choices(n: int) -> list[str]:
    return [f"Choice {i}" for i in range(n)]


def _selects(rows: list[dict]) -> list[dict]:
    return [c for row in rows for c in row["components"] if c["type"] == 3]


def _buttons(rows: list[dict]) -> list[dict]:
    return [c for row in rows for c in row["components"] if c["type"] == 2]


def _itx(
    custom_id: str,
    values: list[str],
    *,
    user: str = "u1",
    guild: str = "",
    channel: str = "c1",
) -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i1",
        interaction_token="tok",
        channel_id=channel,
        user_id=user,
        message_id="m1",
        custom_id=custom_id,
        guild_id=guild,
        values=values,
    )


def _recorder(d: Any) -> list[tuple[str, dict]]:
    turns: list[tuple[str, dict]] = []

    async def _record(msg: Any, **kw: Any) -> None:
        turns.append((msg.text, kw))

    d.handle_message = _record
    return turns


class TestLayout:
    def test_one_choice_is_a_button_with_no_select(self) -> None:
        rows = rc.build_option_rows(["Only"], "t" * 12)
        assert [b["label"] for b in _buttons(rows)] == ["Only"]
        assert _selects(rows) == []

    def test_two_to_twenty_choices_end_in_a_choose_several_select(self) -> None:
        for n in (2, 7, 20):
            rows = rc.build_option_rows(_choices(n), "t" * 12)
            assert len(_buttons(rows)) == n
            (select,) = _selects(rows)
            assert rows[-1]["components"] == [select]
            assert select["placeholder"] == "Choose several…"
            assert (select["min_values"], select["max_values"]) == (1, n)
            assert [o["value"] for o in select["options"]] == [str(i) for i in range(n)]
            assert len(rows) <= 5

    def test_more_than_twenty_choices_become_selects_of_twenty_five(self) -> None:
        rows = rc.build_option_rows(_choices(60), "t" * 12)
        assert _buttons(rows) == []
        selects = _selects(rows)
        assert [len(s["options"]) for s in selects] == [25, 25, 10]
        assert [s["max_values"] for s in selects] == [25, 25, 10]
        assert [s["placeholder"] for s in selects] == [
            "Choose from 1–25",
            "Choose from 26–50",
            "Choose from 51–60",
        ]
        assert selects[2]["options"][0] == {"label": "Choice 50", "value": "50"}

    def test_the_layout_never_passes_discord_limits(self) -> None:
        rows = rc.build_option_rows(_choices(200), "t" * 12)
        assert len(rows) == 5
        assert all(len(s["options"]) <= 25 for s in _selects(rows))
        assert sum(len(s["options"]) for s in _selects(rows)) == rc.MAX_OPTION_CHOICES
        assert all(len(s["custom_id"]) <= 100 for s in _selects(rows))

    def test_a_long_choice_is_cut_to_the_select_label_limit(self) -> None:
        rows = rc.build_option_rows(["x" * 300, "short"], "t" * 12)
        label = _selects(rows)[0]["options"][0]["label"]
        assert len(label) == 100 and label.endswith("…")

    def test_the_renderer_offers_a_long_list_as_selects(self) -> None:
        cli = FakeClient()
        r = DiscordRenderer(cli, "c1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("Pick.\n\n[OPTIONS: " + " | ".join(_choices(30)) + "]")
            await r.on_done()

        asyncio.run(_go())
        selects = _selects(cli.final_components())
        assert [len(s["options"]) for s in selects] == [25, 5]
        assert "26. Choice" not in cli.final_text()


class TestTake:
    def _posted(self, n: int = 3) -> tuple[str, str]:
        tag = "t" * 12
        rows = rc.build_option_rows(_choices(n), tag)
        return _selects(rows)[0]["custom_id"], tag

    def test_a_valid_press_answers_with_the_offered_text_in_order(self) -> None:
        custom_id, tag = self._posted()
        assert rc.take_option_select(custom_id, ["2", "0"]) == (
            rc.SELECT_ANSWERED,
            tag,
            ["Choice 0", "Choice 2"],
        )

    @pytest.mark.parametrize(
        "values",
        [
            [],  # below min_values
            ["0", "0"],  # a repeat
            ["3"],  # not offered
            ["-1"],
            ["Choice 0"],  # free text, even an offered choice's own text
            ["!new"],
            ["０"],  # a non-ASCII digit
        ],
    )
    def test_values_that_do_not_fit_the_menu_are_refused_and_keep_it_live(
        self, values: list[str]
    ) -> None:
        custom_id, _ = self._posted()
        assert rc.take_option_select(custom_id, values)[0] == rc.SELECT_INVALID
        assert rc.take_option_select(custom_id, ["1"])[0] == rc.SELECT_ANSWERED

    def test_more_picks_than_the_menu_holds_are_refused(self) -> None:
        rows = rc.build_option_rows(_choices(30), "t" * 12)
        second = _selects(rows)[1]
        # The second menu holds 26..30, so an index from the first is not its own.
        assert rc.take_option_select(second["custom_id"], ["0"])[0] == rc.SELECT_INVALID
        assert rc.take_option_select(second["custom_id"], ["25", "29"])[2] == [
            "Choice 25",
            "Choice 29",
        ]

    def test_a_menu_index_the_prompt_never_posted_is_refused(self) -> None:
        custom_id, _ = self._posted()
        forged = custom_id.rsplit(":", 1)[0] + ":4"
        assert rc.take_option_select(forged, ["0"])[0] == rc.SELECT_INVALID

    def test_a_prompt_answers_once(self) -> None:
        custom_id, _ = self._posted()
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_ANSWERED
        assert rc.take_option_select(custom_id, ["1"])[0] == rc.SELECT_STALE

    def test_a_newer_prompt_for_the_session_retires_the_older(self) -> None:
        older, tag = self._posted()
        rc.build_option_rows(["New A", "New B"], tag)
        assert rc.take_option_select(older, ["0"])[0] == rc.SELECT_STALE

    def test_a_newer_single_choice_prompt_also_retires_the_older(self) -> None:
        older, tag = self._posted()
        rc.build_option_rows(["Only"], tag)
        assert rc.take_option_select(older, ["0"])[0] == rc.SELECT_STALE

    def test_a_choice_past_the_length_ceiling_is_not_kept_and_not_sent(self) -> None:
        tag = "t" * 12
        huge = "y" * (rc._FULL_CHOICE_MAX_CHARS + 1)
        at_cap = "z" * rc._FULL_CHOICE_MAX_CHARS
        custom_id = _selects(rc.build_option_rows([huge, at_cap], tag))[0]["custom_id"]
        (entry,) = rc._OPTION_SELECTS.values()
        assert entry.choices == ("", at_cap)
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_INVALID
        assert rc.take_option_select(custom_id, ["1"]) == (rc.SELECT_ANSWERED, tag, [at_cap])

    def test_another_sessions_prompt_does_not_retire_this_one(self) -> None:
        custom_id, _ = self._posted()
        rc.build_option_rows(["A", "B"], "o" * 12)
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_ANSWERED

    def test_the_next_turn_retires_the_prompt_once_it_holds_the_session(self) -> None:
        """Built before the claim, so a menu the turn ahead posts while this one
        waits is still the newer prompt then; the claim's hook retires it."""
        tag = session_provenance_tag("sk")
        r = DiscordRenderer(FakeClient(), "c1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        r.bind_context_source(None)
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_STALE

    @pytest.mark.asyncio
    async def test_a_typed_discord_turn_retires_the_sessions_menus(self) -> None:
        from discord_helpers import _inbound

        d, cli, sess = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        await d.handle_message(_inbound("never mind, status only"))
        assert sess.begin_turns == 1
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_STALE

    def test_after_a_restart_a_replayed_select_answers_nothing(self, monkeypatch) -> None:
        custom_id, _ = self._posted()
        monkeypatch.setattr(rc, "_OPTION_SELECTS", OrderedDict())
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_STALE


class TestPress:
    @pytest.mark.asyncio
    async def test_a_multi_select_answers_as_one_turn_with_its_full_text(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        long_b = "Run the full smoke suite against every region " + "x" * 80
        rows = rc.build_option_rows(["Alpha", long_b, "Gamma"], tag)
        turns = _recorder(d)

        await d.on_interaction(_itx(_selects(rows)[0]["custom_id"], ["1", "0"]))

        assert cli.acked == ["i1"]
        assert cli.component_edits == [("m1", [])]
        assert f"> Alpha, {long_b}" in [t for t, _ in cli.sent]
        assert [t for t, _ in turns] == [f"Alpha, {long_b}"]
        # Model-authored text: turn content routed by its tag, never a command.
        assert turns[0][1] == {"interpret_commands": False, "origin_tag": tag}

    @pytest.mark.asyncio
    async def test_a_select_press_runs_a_real_turn(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        rows = rc.build_option_rows(["Choice A", "Choice B"], tag)
        await d.on_interaction(_itx(_selects(rows)[0]["custom_id"], ["1"]))
        shown = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert any("Answer: Choice B" in t for t in shown)
        assert sess.begin_turns == 1

    @pytest.mark.asyncio
    async def test_an_unauthorized_presser_is_dropped_and_the_prompt_stays_live(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        turns = _recorder(d)

        await d.on_interaction(_itx(custom_id, ["0"], user="intruder"))

        assert cli.acked == [] and cli.sent == [] and turns == []
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_ANSWERED

    @pytest.mark.asyncio
    async def test_a_guild_channel_off_the_thread_roster_is_dropped(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        turns = _recorder(d)

        await d.on_interaction(_itx(custom_id, ["0"], guild="g1"))

        assert cli.acked == [] and turns == []

    @pytest.mark.asyncio
    async def test_a_governance_deny_drops_the_press_before_it_is_taken(self, monkeypatch) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        turns = _recorder(d)

        async def _deny(_channel: str) -> bool:
            return False

        monkeypatch.setattr(td_mod, "channel_inbound_permitted", _deny)
        await d.on_interaction(_itx(custom_id, ["0"]))

        assert cli.acked == ["i1"] and turns == [] and cli.component_edits == []
        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_ANSWERED

    @pytest.mark.asyncio
    async def test_authorization_withdrawn_during_the_ack_drops_the_press(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        turns = _recorder(d)
        original_ack = cli.ack_component_interaction

        async def _ack_then_revoke(*args: Any, **kwargs: Any) -> None:
            await original_ack(*args, **kwargs)
            d._allowed.discard("u1")

        cli.ack_component_interaction = _ack_then_revoke  # type: ignore[method-assign]
        await d.on_interaction(_itx(custom_id, ["0"]))

        assert turns == [] and cli.component_edits == []

    @pytest.mark.asyncio
    async def test_values_the_menu_did_not_offer_start_no_turn(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        turns = _recorder(d)

        await d.on_interaction(_itx(custom_id, ["!new"]))
        await d.on_interaction(_itx(custom_id, ["0", "1", "1"]))
        await d.on_interaction(_itx(custom_id, []))

        assert turns == []
        assert sum("Couldn't read that choice" in t for t, _ in cli.sent) == 3

    @pytest.mark.asyncio
    async def test_a_second_press_on_an_answered_prompt_is_refused(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        custom_id = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        turns = _recorder(d)

        await d.on_interaction(_itx(custom_id, ["0"]))
        await d.on_interaction(_itx(custom_id, ["1"]))

        assert [t for t, _ in turns] == ["A"]
        assert any("no longer active" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_a_select_from_an_older_prompt_is_refused(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        older = _selects(rc.build_option_rows(["A", "B"], tag))[0]["custom_id"]
        rc.build_option_rows(["C", "D"], tag)
        turns = _recorder(d)

        await d.on_interaction(_itx(older, ["0"]))

        assert turns == []
        assert cli.component_edits == [("m1", [])]
        assert any("no longer active" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_a_select_another_session_posted_is_refused_by_routing(self) -> None:
        """The tag is checked against the conversation's current target, as for a
        button: a menu the session behind a `!new` posted runs nowhere."""
        d, cli, sess = _dispatcher({"u1"})
        old_tag = session_provenance_tag("discord:some-earlier-session")
        custom_id = _selects(rc.build_option_rows(["A", "B"], old_tag))[0]["custom_id"]

        await d.on_interaction(_itx(custom_id, ["0"]))

        assert any("moved away" in t for t, _ in cli.sent)
        assert sess.begin_turns == 0

    @pytest.mark.asyncio
    async def test_an_untagged_select_fails_closed(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        custom_id = _selects(rc.build_option_rows(["A", "B"], ""))[0]["custom_id"]
        turns = _recorder(d)

        await d.on_interaction(_itx(custom_id, ["0"]))

        assert turns == []
        assert any("predate" in t for t, _ in cli.sent)


class TestGatewayValues:
    def _dispatch(self, values: Any) -> DiscordInteraction:
        from kiro_crew.discord.client import DiscordClient

        seen: list[DiscordInteraction] = []

        async def _handler(interaction: DiscordInteraction) -> None:
            seen.append(interaction)

        client = DiscordClient(token="bot-secret", on_interaction=_handler)

        async def _go() -> None:
            client._on_dispatch(
                "INTERACTION_CREATE",
                {
                    "id": 1,
                    "token": "tok",
                    "type": 3,
                    "channel_id": 2,
                    "user": {"id": 4},
                    "data": {"custom_id": "sel:abc:0", "component_type": 3, "values": values},
                    "message": {"id": 5, "components": []},
                },
            )
            await asyncio.gather(*tuple(client._handler_tasks))

        asyncio.run(_go())
        return seen[0]

    def test_a_select_press_carries_its_values(self) -> None:
        assert self._dispatch(["3", "0"]).values == ["3", "0"]

    @pytest.mark.parametrize("values", ["0", [0], ["0", None], [str(i) for i in range(26)]])
    def test_malformed_values_are_dropped_whole(self, values: Any) -> None:
        assert self._dispatch(values).values == []


class TestDashboardTurn:
    @pytest.mark.asyncio
    async def test_a_dashboard_turn_on_the_session_retires_its_menus(self, tmp_path) -> None:
        """A Discord conversation bound to a dashboard session answers there too:
        a dashboard turn moves the question on just as a Discord turn does."""
        from chat_runner_coverage_helpers import (
            _complete,
            _drive,
            _runner_state,
            _set_stream,
            _slot,
        )

        from kiro_crew.dashboard import chat_runner

        state, client = _runner_state(tmp_path)
        slot = _slot("discord-bound")
        state._slots[slot.key] = slot
        session_key = chat_runner.effective_session_key(slot)
        tag = session_provenance_tag(session_key)
        custom_id = _selects(rc.build_option_rows(["Ship", "Hold"], tag))[0]["custom_id"]
        other = _selects(rc.build_option_rows(["A", "B"], "o" * 12))[0]["custom_id"]
        _set_stream(client, [_complete()])

        await _drive(state, slot, "answered on the dashboard instead")

        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_STALE
        assert rc.take_option_select(other, ["0"])[0] == rc.SELECT_ANSWERED

    @pytest.mark.asyncio
    async def test_menus_posted_while_the_dashboard_waits_for_the_session_are_retired(
        self, tmp_path
    ) -> None:
        """A Discord turn still answering when the dashboard turn queues behind it
        posts its menus during that wait; the dashboard turn supersedes them."""
        from unittest.mock import AsyncMock

        from chat_runner_coverage_helpers import (
            _complete,
            _drive,
            _runner_state,
            _set_stream,
            _slot,
        )

        from kiro_crew.dashboard import chat_runner

        state, client = _runner_state(tmp_path)
        slot = _slot("discord-bound-busy")
        state._slots[slot.key] = slot
        session_key = chat_runner.effective_session_key(slot)
        tag = session_provenance_tag(session_key)
        posted: list[str] = []

        async def _claim_after_the_discord_turn_ends(*_a: Any, **_kw: Any) -> Any:
            rows = rc.build_option_rows(["Deploy", "Hold"], tag)
            posted.append(_selects(rows)[0]["custom_id"])
            return client, False, False

        state.sessions.get_or_create = AsyncMock(side_effect=_claim_after_the_discord_turn_ends)
        _set_stream(client, [_complete()])

        await _drive(state, slot, "ignore those choices; status only")

        assert posted
        assert rc.take_option_select(posted[0], ["0"])[0] == rc.SELECT_STALE

    @pytest.mark.asyncio
    async def test_an_expanded_saved_prompt_turn_retires_the_menus(self, tmp_path) -> None:
        from chat_runner_coverage_helpers import (
            _complete,
            _quiet_sel,
            _runner_state,
            _set_stream,
            _settle,
            _slot,
        )

        from kiro_crew.dashboard import chat_runner

        state, client = _runner_state(tmp_path)
        slot = _slot("discord-bound-prompt")
        state._slots[slot.key] = slot
        tag = session_provenance_tag(chat_runner.effective_session_key(slot))
        custom_id = _selects(rc.build_option_rows(["Deploy", "Hold"], tag))[0]["custom_id"]
        _set_stream(client, [_complete()])
        slot._empty_response_retries = 2

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "the saved prompt's text", _prompt_depth=1)
        await _settle(slot)

        assert rc.take_option_select(custom_id, ["0"])[0] == rc.SELECT_STALE
