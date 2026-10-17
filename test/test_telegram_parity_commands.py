"""Telegram's command surface: what each command parses to and replies.

The menu and the help card stay one source; ``/status``, ``/cron``, ``/spawn``,
``/task run``, ``/title``, ``/sessions``, ``/agent`` and the dashboard link answer
through the shared replies; the agent picker's keyboard, cut and picks; and who
may receive a host-wide listing, which a forum Topic is refused. The doubles come
from ``test_telegram`` and the shared messages from ``telegram_parity_helpers.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram_parity_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _AWS_KEY,
    _drop_live_config_snapshot,
    _forum_msg,
    _msg,
)
from test_telegram import _dispatcher, _dm

from kiro_crew.telegram.commands import COMMAND_SPEC, bot_command_payload, parse_command

# ---------------------------------------------------------------------------
# The command surface
# ---------------------------------------------------------------------------


class TestCommandSurface:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("/agent", "agent"),
            ("/agents", "agent"),
            ("/status", "status"),
            ("/ping", "ping"),
            ("/sessions", "sessions"),
            ("/session launch plan", "sessions"),
            ("/title rename me", "title"),
            ("/cron list", "cron"),
            ("/crons list", "cron"),
            ("/spawn do it", "spawn"),
            ("/bg do it", "spawn"),
            ("/task run x", "task"),
            ("/tasks status", "task"),
        ],
    )
    def test_every_new_command_parses(self, text: str, expected: str) -> None:
        assert parse_command(text) == expected

    def test_the_menu_and_the_help_card_stay_one_source(self) -> None:
        names = {name for name, _ in COMMAND_SPEC}
        assert {"agent", "status", "session", "cron"} <= names
        # Every row must survive the Bot API's own constraints, or Telegram
        # rejects the WHOLE array and the user loses the entire menu.
        payload = bot_command_payload()
        assert len(payload) == len(COMMAND_SPEC)
        for row in payload:
            assert row["command"].islower() and row["description"]

    @pytest.mark.parametrize("cmd", ["/spawn", "/bg", "/task", "/cron"])
    def test_a_menu_absent_command_is_one_that_needs_an_argument(self, cmd: str) -> None:
        # /spawn, /bg and /task take a mandatory argument, so a menu tap (which
        # SENDS the bare token) would put a dead entry in the list. /cron lists.
        names = {f"/{name}" for name, _ in COMMAND_SPEC}
        assert (cmd in names) is (cmd == "/cron")


class TestServiceCommands:
    @pytest.mark.asyncio
    async def test_status_and_ping_answer_without_a_session(self) -> None:
        dispatcher, client, sessions = _dispatcher({1})
        await dispatcher.handle_message(_msg("/status"))
        await dispatcher.handle_message(_msg("/ping"))
        replies = [text for text, _ in client.sent]
        assert "uptime" in replies[0] and replies[1] == "pong"
        # Neither may start a turn: the point of /ping is proving the gateway is
        # alive without depending on a provider that may be the wedged thing.
        assert sessions.successes == [] and sessions.released == []

    @pytest.mark.parametrize(
        "cmd,attr",
        [
            ("/cron list", "cron_service"),
            ("/spawn x", "subagent_manager"),
            ("/task status", "task_runner"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_absent_service_says_so_rather_than_failing_mute(
        self, cmd: str, attr: str
    ) -> None:
        dispatcher, client, _ = _dispatcher({1})
        assert getattr(dispatcher, attr) is None
        await dispatcher.handle_message(_msg(cmd))
        assert "not " in client.sent[-1][0].lower()

    @pytest.mark.asyncio
    async def test_cron_reaches_the_shared_reply(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        dispatcher.cron_service = MagicMock()
        dispatcher.cron_service.list_jobs.return_value = []
        await dispatcher.handle_message(_msg("/cron list"))
        assert client.sent[-1][0] == "No cron jobs scheduled."

    @pytest.mark.asyncio
    async def test_a_bare_cron_answers_with_its_usage(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        dispatcher.cron_service = MagicMock()
        await dispatcher.handle_message(_msg("/cron"))
        assert "Usage:" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_spawn_carries_the_session_key_as_the_parent(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        manager = MagicMock(max_concurrent=2)
        manager.spawn.return_value = SimpleNamespace(id="s1")
        dispatcher.subagent_manager = manager
        await dispatcher.handle_message(_msg("/spawn reindex"))
        assert manager.spawn.call_args.kwargs["parent_session_key"].startswith("telegram:")
        assert "s1" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_task_run_reaches_the_shared_reply(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        runner = MagicMock()
        runner.status.return_value = {"running": False}
        dispatcher.task_runner = runner
        await dispatcher.handle_message(_msg("/task status"))
        assert client.sent[-1][0] == "No task running."


class TestTaskRunCommand:
    @pytest.mark.asyncio
    async def test_the_documented_run_form_actually_starts_the_runner(self, tmp_path: Any) -> None:
        # The keyword grammar spells the verb "task run", but /task delivers
        # "run <spec>" as its ARGUMENT — re-composing "task run " + arg handed the
        # runner a spec named "run <spec>", so the headline form never worked.
        spec = tmp_path / "plan.yaml"
        spec.write_text("steps: []", encoding="utf-8")
        dispatcher, client, _ = _dispatcher({1})
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        dispatcher.task_runner = runner
        await dispatcher.handle_message(_msg(f"/task run {spec}"))
        assert runner.start_background.await_args.args[0].name == "plan.yaml"
        assert "plan.yaml" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_bare_run_answers_with_its_usage(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        runner = MagicMock(running=False)
        runner.start_background = AsyncMock()
        dispatcher.task_runner = runner
        await dispatcher.handle_message(_msg("/task run"))
        assert "Usage:" in client.sent[-1][0]
        runner.start_background.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_shared_replies_arrive_rendered_not_literal(self) -> None:
        # The shared reply text is markdown Slack renders natively; posted
        # plaintext its asterisks and backticks reach the user verbatim.
        dispatcher, client, _ = _dispatcher({1})
        manager = MagicMock(max_concurrent=2)
        manager.spawn.return_value = SimpleNamespace(id="s1")
        dispatcher.subagent_manager = manager
        await dispatcher.handle_message(_msg("/spawn reindex"))
        text = client.sent[-1][0]
        assert "<code>s1</code>" in text and "`" not in text


class TestTitleCommand:
    @pytest.mark.asyncio
    async def test_a_title_is_recorded_against_this_conversation(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        log = MagicMock()
        dispatcher.conv_log = log
        await dispatcher.handle_message(_msg("/title Quarterly review"))
        assert log.set_title.call_args.args[1] == "Quarterly review"
        assert "Quarterly review" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_an_empty_title_answers_with_its_usage(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        dispatcher.conv_log = MagicMock()
        await dispatcher.handle_message(_msg("/title    "))
        assert "Usage:" in client.sent[-1][0]
        dispatcher.conv_log.set_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_title_is_redacted_collapsed_and_capped(self) -> None:
        dispatcher, _, _ = _dispatcher({1})
        log = MagicMock()
        dispatcher.conv_log = log
        await dispatcher.handle_message(_msg(f"/title a\n\nb  c {_AWS_KEY} " + "z" * 200))
        title = log.set_title.call_args.args[1]
        assert _AWS_KEY not in title and "\n" not in title and len(title) <= 80

    @pytest.mark.asyncio
    async def test_a_write_failure_is_reported_not_raised(self) -> None:
        dispatcher, client, _ = _dispatcher({1})
        log = MagicMock()
        log.set_title.side_effect = OSError("read-only")
        dispatcher.conv_log = log
        await dispatcher.handle_message(_msg("/title x"))
        assert "Couldn't rename" in client.sent[-1][0]


class TestAgentSwitchSafety:
    @pytest.mark.asyncio
    async def test_a_switch_is_refused_while_a_reply_is_streaming(self) -> None:
        # The agent is part of the session key, so switching mid-turn would move
        # the key out from under a running turn: _active_renderers, the queue
        # receipt and /stop's provider lookup all key on it, so that turn would
        # keep streaming with no route back to it.
        dispatcher, _, sessions = _dispatcher({1})
        route = ("direct", "1")
        sessions._busy = True
        before = dispatcher._session_key(route)
        outcome = await dispatcher._apply_agent(route, "alpha")
        assert "Still working" in outcome
        assert dispatcher._session_key(route) == before
        assert dispatcher._agent_picks.get(route) == ""


class TestSessionsCommand:
    @staticmethod
    def _install_log(dispatcher: Any, log: Any) -> None:
        dispatcher.conv_log = log
        dispatcher._session_resume.conv_log = log
        dispatcher._session_resume._controller.conv_log = log

    @pytest.mark.asyncio
    async def test_singular_alias_fuzzy_searches_and_posts_buttons(self, tmp_path: Any) -> None:
        from kiro_crew.history import ConversationLog

        dispatcher, client, _ = _dispatcher({1})
        log = ConversationLog(base_dir=tmp_path / "sessions")
        await asyncio.to_thread(
            log.append,
            "dashboard:matching",
            "user",
            "The blue marmot owns the launch checklist",
        )
        await asyncio.to_thread(log.set_title, "dashboard:matching", "General Q&A")
        await asyncio.to_thread(log.append, "dashboard:other", "user", "Different topic")
        await asyncio.to_thread(log.set_title, "dashboard:other", "Other session")
        self._install_log(dispatcher, log)

        await dispatcher.handle_message(_msg("/session blue marmot"))

        heading, markup = client.sent[-1]
        labels = [row[0]["text"] for row in markup["inline_keyboard"]]
        assert "Session search" in heading
        assert any("General Q&A" in label for label in labels)
        assert all("Other session" not in label for label in labels)

    @pytest.mark.asyncio
    async def test_empty_history_says_so(self, tmp_path: Any) -> None:
        from kiro_crew.history import ConversationLog

        dispatcher, client, _ = _dispatcher({1})
        self._install_log(dispatcher, ConversationLog(base_dir=tmp_path / "sessions"))

        await dispatcher.handle_message(_msg("/session"))

        assert client.sent[-1][0] == "No recent sessions."

    @pytest.mark.asyncio
    async def test_search_failure_is_audited_and_fails_closed(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging import session_resume as resume_core

        dispatcher, client, _ = _dispatcher({1})
        log = ConversationLog(base_dir=tmp_path / "sessions")
        self._install_log(dispatcher, log)
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(
            resume_core,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: seen.append(kw)),
        )
        monkeypatch.setattr(log, "search_sessions", MagicMock(side_effect=OSError("gone")))

        await dispatcher.handle_message(_msg("/session launch"))

        assert seen and seen[-1]["outcome"] == "error"
        assert seen[-1]["source"] == "telegram"
        assert client.sent[-1][0] == "⚠️ Recent sessions are unavailable."


class TestAgentPicker:
    @pytest.mark.asyncio
    async def test_a_keyboard_lists_the_installed_specs_plus_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, client, _ = _dispatcher({1})
        monkeypatch.setattr(
            type(dispatcher), "_installed_agent_names", staticmethod(lambda: ["alpha", "beta"])
        )
        await dispatcher.handle_message(_msg("/agent"))
        _text, markup = client.sent[-1]
        labels = [row[0]["text"] for row in markup["inline_keyboard"]]
        assert any("Default" in label for label in labels)
        assert "alpha" in labels and "beta" in labels

    @pytest.mark.asyncio
    async def test_no_installed_specs_answers_rather_than_posting_a_dead_keyboard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, client, _ = _dispatcher({1})
        monkeypatch.setattr(type(dispatcher), "_installed_agent_names", staticmethod(lambda: []))
        await dispatcher.handle_message(_msg("/agent"))
        text, markup = client.sent[-1]
        assert "No agent list" in text and markup is None

    @pytest.mark.asyncio
    async def test_a_discovery_failure_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, client, _ = _dispatcher({1})

        def _boom() -> Any:
            raise OSError("agents dir gone")

        monkeypatch.setattr(type(dispatcher), "_installed_agent_names", staticmethod(_boom))
        await dispatcher.handle_message(_msg("/agent"))
        assert "No agent list" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_pick_changes_the_session_key_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, client, sessions = _dispatcher({1})
        monkeypatch.setattr(
            type(dispatcher), "_installed_agent_names", staticmethod(lambda: ["alpha"])
        )
        await dispatcher.handle_message(_msg("/agent"))
        route = ("direct", "1")
        before = dispatcher._session_key(route)
        assert sessions.has_session(before)  # a live conversation to warn about
        outcome = await dispatcher._apply_agent(route, "alpha")
        after = dispatcher._session_key(route)
        # The agent is part of the key, so a pick necessarily leaves this one.
        assert after != before and "alpha" in after
        assert "leaves the current conversation" in outcome

    @pytest.mark.asyncio
    async def test_picking_the_default_row_clears_the_preference(self) -> None:
        dispatcher, _, _ = _dispatcher({1})
        route = ("direct", "1")
        await dispatcher._apply_agent(route, "alpha")
        assert dispatcher._resolve_agent(route) == "alpha"
        await dispatcher._apply_agent(route, "")
        assert dispatcher._agent_picks.get(route) == ""
        assert dispatcher._resolve_agent(route) == "kirocrew"

    @pytest.mark.asyncio
    async def test_a_pick_follows_the_new_agents_own_generations(self) -> None:
        # After a restart nothing is in memory: the route's counter was seeded
        # from the default agent's bucket (gen 0) while alpha's bucket already
        # holds gen 1 and 2 on disk. The pick must land on alpha's latest (gen
        # 2) so /new goes to gen 3, never back onto alpha's gen 1 or 2.
        dispatcher, client, sessions = _dispatcher({1})
        route = ("direct", "1")
        sessions._has = False
        persisted = {"kirocrew": 0, "alpha": 2}
        sessions.max_generation = lambda bucket: persisted[bucket.split(":")[1]]
        reserved: list[str] = []
        sessions.reserve_generation = reserved.append
        assert dispatcher._conv.current_gen(route) == 0
        assert dispatcher._session_key(route) == "telegram:kirocrew:direct:1"

        assert await dispatcher._apply_agent(route, "alpha") == "✅ Agent set to alpha."
        assert dispatcher._session_key(route) == "telegram:alpha:direct:1:gen2"

        await dispatcher.handle_message(_msg("/new"))
        assert client.sent[-1][0] == "✅ New conversation started."
        assert dispatcher._session_key(route) == "telegram:alpha:direct:1:gen3"
        assert reserved == ["telegram:alpha:direct:1:gen3"]

        # Back to the default row: its own latest again, not alpha's counter.
        assert await dispatcher._apply_agent(route, "") == "✅ Agent set to Default (kirocrew)."
        assert dispatcher._session_key(route) == "telegram:kirocrew:direct:1"

    @pytest.mark.asyncio
    async def test_pressing_the_current_row_is_not_a_switch(self) -> None:
        # The in-memory generation can be AHEAD of disk (a /new whose save
        # failed, a rotation). Pressing the row already marked "•" must not
        # reseed from disk, or the next message would resume the conversation
        # the user had already left and a pending compact prompt would be lost.
        dispatcher, _, sessions = _dispatcher({1})
        route = ("direct", "1")
        sessions.max_generation = lambda bucket: 2
        await dispatcher._apply_agent(route, "alpha")
        dispatcher._conv.bump_gen(route)  # a /new that never reached disk
        dispatcher._conv.set_awaiting(route)
        key = dispatcher._session_key(route)
        assert key == "telegram:alpha:direct:1:gen3"

        assert await dispatcher._apply_agent(route, "alpha") == "ℹ️ Already using alpha."
        assert dispatcher._session_key(route) == key
        assert dispatcher._conv.is_awaiting(route)

        # The Default row with no pick is the same no-op, compared as the agent
        # each side RUNS rather than as the raw pick.
        await dispatcher._apply_agent(route, "")
        dispatcher._conv.bump_gen(route)
        key = dispatcher._session_key(route)
        assert await dispatcher._apply_agent(route, "") == "ℹ️ Already using Default (kirocrew)."
        assert dispatcher._session_key(route) == key
        assert await dispatcher._apply_agent(route, "kirocrew") == ("ℹ️ Already using kirocrew.")
        assert dispatcher._session_key(route) == key

    @pytest.mark.asyncio
    async def test_a_double_press_applies_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatcher, client, _ = _dispatcher({1})
        monkeypatch.setattr(
            type(dispatcher), "_installed_agent_names", staticmethod(lambda: ["alpha"])
        )
        await dispatcher.handle_message(_msg("/agent"))
        message_id = 101  # FakeClient's first minted id
        cb = SimpleNamespace(
            callback_query_id="q1",
            user_id=1,
            chat_id=1,
            chat_type="private",
            message_id=message_id,
            data="g:1",
            label="",
            message_thread_id=None,
        )
        await dispatcher.on_callback(cb)
        first = client.edits[-1][1]
        await dispatcher.on_callback(cb)
        second = client.edits[-1][1]
        assert "Agent set to alpha" in first
        assert "no longer active" in second

    @pytest.mark.asyncio
    async def test_an_out_of_range_index_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatcher, client, _ = _dispatcher({1})
        monkeypatch.setattr(
            type(dispatcher), "_installed_agent_names", staticmethod(lambda: ["alpha"])
        )
        await dispatcher.handle_message(_msg("/agent"))
        for data in ("g:99", "g:-1", "g:notanint"):
            await dispatcher.on_callback(
                SimpleNamespace(
                    callback_query_id="q",
                    user_id=1,
                    chat_id=1,
                    chat_type="private",
                    message_id=101,
                    data=data,
                    label="",
                    message_thread_id=None,
                )
            )
            assert "no longer active" in client.edits[-1][1]

    def test_the_picker_table_is_bounded_in_both_directions(self) -> None:
        import time as _time

        from kiro_crew.telegram.transport_dispatch import (
            _MODEL_PICKER_MAX,
            _MODEL_PICKER_TTL_SECS,
            TelegramDispatcher,
            _Picker,
        )

        now = _time.time()
        table = {
            f"c:{i}": _Picker(route=("direct", "1"), created_at=now - i, choices=())
            for i in range(_MODEL_PICKER_MAX + 20)
        }
        table["stale"] = _Picker(
            route=("direct", "1"), created_at=now - _MODEL_PICKER_TTL_SECS - 1, choices=()
        )
        TelegramDispatcher._prune_pickers(table, now)
        assert "stale" not in table and len(table) <= _MODEL_PICKER_MAX

    @staticmethod
    def _agent(name: str, filename: str, *, source: str = "builtin", owned: bool = False) -> Any:
        from kiro_crew.agent_discovery import AgentInfo

        return AgentInfo(
            name=name,
            filename=filename,
            description="",
            model="auto",
            source=source,
            kirocrew_owned=owned,
        )

    def test_internal_and_app_agents_are_hidden_from_the_picker(self) -> None:
        """The reported harm: Kiro Crew internals and app agents occupy slots and
        push the user's own agents out. Filtering is a property of the roster
        row, so a user's own ``kirocrew``-prefixed agent (not owned) stays."""
        from kiro_crew.telegram import transport_dispatch as td

        roster = [
            # Kiro Crew-generated internals — owned, so hidden regardless of source.
            self._agent("kirocrew", "kirocrew.json", source="kirocrew", owned=True),
            self._agent("kirocrew-heartbeat", "kirocrew-heartbeat.json", owned=True),
            self._agent("kirocrew-worker", "kirocrew-worker.json", owned=True),
            # An app-installed agent: materialised under the "<app>--<agent>.json"
            # link filename, declared name may be bare.
            self._agent("companion", "crew-companion--companion.json", source="package"),
            # A user's OWN agents — the ones the reporter actually uses.
            self._agent("vitrina-designer", "vitrina-designer.json"),
            self._agent("vitrina-writer", "vitrina-writer.json"),
            # A user's hand-authored kirocrew-prefixed agent: NOT owned, so kept.
            self._agent("kirocrew-custom", "kirocrew-custom.json"),
            # A genuine AIM package agent the user installed: kept (not an app link).
            self._agent("customer360", "Customer360Context-customer360.json", source="package"),
        ]
        with patch.object(td, "list_agents", lambda: roster):
            names = td.TelegramDispatcher._installed_agent_names()

        assert names == [
            "customer360",
            "kirocrew-custom",
            "vitrina-designer",
            "vitrina-writer",
        ]
        for hidden in ("kirocrew", "kirocrew-heartbeat", "kirocrew-worker", "companion"):
            assert hidden not in names

    @pytest.mark.asyncio
    async def test_truncation_is_surfaced_instead_of_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """More selectable agents than fit: the cut must be VISIBLE. A trailing
        row names how many were dropped, and it is not a pressable ``g:`` index."""
        from kiro_crew.telegram.transport_dispatch import _PICKER_LIMIT

        dispatcher, client, _ = _dispatcher({1})
        overflow = 5
        names = [f"agent{i:03d}" for i in range(_PICKER_LIMIT + overflow)]
        monkeypatch.setattr(type(dispatcher), "_installed_agent_names", staticmethod(lambda: names))
        await dispatcher.handle_message(_msg("/agent"))
        _text, markup = client.sent[-1]
        rows = markup["inline_keyboard"]
        labels = [row[0]["text"] for row in rows]
        notices = [label for label in labels if "not shown" in label]
        assert notices == [f"… and {overflow} more not shown"]
        # The notice row is inert: its callback is not a g:<index> the picker
        # resolves, so a press cannot silently pick an agent.
        notice_row = next(row for row in rows if "not shown" in row[0]["text"])
        assert notice_row[0]["callback_data"] == "noop"
        # Only the LIMIT selectable agents (plus the Default row) are pressable.
        pressable = [row for row in rows if row[0]["callback_data"].startswith("g:")]
        assert len(pressable) == _PICKER_LIMIT + 1

    def test_no_truncation_notice_when_agents_fit(self) -> None:
        dispatcher, _, _ = _dispatcher({1})
        choices = dispatcher._agent_choices(["alpha", "beta"])
        # ``_agent_choices`` never carries the notice — it holds only resolvable
        # rows, so the notice cannot become a pressable index.
        assert all(label != "" for _v, label in choices)
        assert not any("not shown" in label for _v, label in choices)


class TestSharedSessionsCollector:
    def test_the_slack_wrapper_threads_its_OWN_data_home_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # The collector moved to messaging/, but the Slack module keeps the
        # _SESSIONS_DIR name that its suites and the lazy-data-home ratchet patch.
        # A patch that set an attribute nothing reads would leave those tests
        # passing while the read hit the operator's real home.
        from kiro_crew.slack import sessions_view as sv

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "dashboard_abc.jsonl").write_text(
            '{"_type": "metadata", "title": "Planted"}\n' '{"role": "user", "content": "hi"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(sv, "_SESSIONS_DIR", sessions)
        rows = sv._collect_recent_sessions(None, limit=5)
        assert [row["title"] for row in rows] == ["Planted"]

    def test_the_neutral_collector_takes_an_explicit_directory(self, tmp_path: Any) -> None:
        from kiro_crew.messaging.sessions_view import _collect_recent_sessions

        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "telegram_x.jsonl").write_text(
            '{"_type": "metadata", "title": "Neutral"}\n', encoding="utf-8"
        )
        rows = _collect_recent_sessions(None, limit=5, sessions_dir=sessions)
        assert [row["title"] for row in rows] == ["Neutral"]

    def test_an_absent_directory_is_empty_not_an_error(self, tmp_path: Any) -> None:
        from kiro_crew.messaging.sessions_view import _collect_recent_sessions

        assert _collect_recent_sessions(None, sessions_dir=tmp_path / "nope") == []


class TestSessionsIsDirectMessageOnly:
    """`/sessions` names every conversation on the host, so its AUDIENCE matters.

    A forum Topic is readable by the whole supergroup, while the allow-list only
    gates who may drive a turn. Answering there would disclose the operator's
    conversation titles to members who were never allow-listed at all. Slack's
    equivalent has no such shape (its reply lands in a DM or a thread the caller is
    already in), so this is the Telegram-specific half of the same rule.
    """

    @pytest.mark.asyncio
    async def test_a_forum_topic_is_refused_and_lists_nothing(self) -> None:
        dispatcher, client, _ = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        picker = AsyncMock()
        dispatcher._session_resume.show_picker = picker

        await dispatcher.handle_message(_forum_msg("/sessions"))

        # The refusal is the point, but so is not having READ the directory: the
        # picker owns the read and must never be called for a shared Topic.
        picker.assert_not_awaited()
        reply = client.sent[-1][0]
        assert "direct message" in reply
        # No titles leak through the refusal itself.
        assert "\n-" not in reply

    @pytest.mark.asyncio
    async def test_a_direct_message_still_lists(self) -> None:
        """Non-vacuity: the gate must refuse the Topic, not the command."""
        dispatcher, client, _ = _dispatcher({1})
        await dispatcher.handle_message(_msg("/sessions"))
        reply = client.sent[-1][0]
        assert "direct message" not in reply

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command", ["/sessions", "/session launch", "/cron list", "/spawn list"]
    )
    async def test_a_listing_needs_an_unambiguous_owner_even_in_a_dm(self, command: str) -> None:
        """A DM is the right audience; it also has to be the right PERSON.

        `allowed_user_ids` is a list of people permitted to talk to the agent, not a
        claim that one of them owns the install. With several entries, a listing of
        every conversation on the host hands one allow-listed human another's
        conversation titles — under the default per-peer `dm_scope` those are
        separate sessions belonging to separate people. Same rule the owner
        notification follows, which cites `/sessions` as its own premise.
        """
        dispatcher, client, sessions = _dispatcher({7, 8})
        dispatcher.cron_service = MagicMock()
        dispatcher.subagent_manager = MagicMock()
        picker = AsyncMock()
        dispatcher._session_resume.show_picker = picker

        await dispatcher.handle_message(_dm(command))

        # Refused BEFORE any picker or host listing is built.
        picker.assert_not_awaited()
        assert not dispatcher.cron_service.method_calls
        assert "single operator" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_sole_operator_still_gets_the_listing(self) -> None:
        """Non-vacuity, and the common case: one configured identity is unambiguous."""
        dispatcher, client, _ = _dispatcher({7})
        await dispatcher.handle_message(_dm("/sessions"))
        assert "single operator" not in client.sent[-1][0]
        assert "direct message" not in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_turn_and_the_non_listing_commands_are_untouched(self) -> None:
        """The rule is scoped to the LISTINGS, not to a multi-person install.

        A two-person allow-list still chats, still stops, still spawns — refusing
        those would turn one disclosure rule into a rewrite of who may use the bot.
        """
        dispatcher, client, _ = _dispatcher({7, 8})
        dispatcher.subagent_manager = MagicMock()
        dispatcher.subagent_manager.spawn.return_value = SimpleNamespace(id="sa-1")

        await dispatcher.handle_message(_dm("hello there"))
        assert any(t.startswith("Answer:") for t, _ in client.sent), "a plain turn must run"

        await dispatcher.handle_message(_dm("/spawn summarise the log"))
        assert "single operator" not in client.sent[-1][0]
        dispatcher.subagent_manager.spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_cron_listing_is_refused_in_a_topic_too(self) -> None:
        # `/cron list` names every job on the host, which is the same audience
        # problem `/sessions` has, so it goes through the same gate.
        dispatcher, client, _ = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        service = MagicMock()
        dispatcher.cron_service = service
        await dispatcher.handle_message(_forum_msg("/cron list"))
        # Refused BEFORE the service was asked: a listing that was built and then
        # not sent still read host state on behalf of a Topic.
        service.assert_not_called()
        assert not service.method_calls
        assert "direct message" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_forum_topic_still_reaches_the_commands_that_act_on_it(self) -> None:
        """The gate is scoped to the listings, not to "host-wide" as a category.

        `/spawn <task>` acts on THIS conversation's session and reports its own work,
        so refusing it would break the forum surface for no disclosure gain. Pinning
        it keeps a later "harden everything" edit from quietly widening the gate.
        """
        dispatcher, client, _ = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        manager = MagicMock()
        manager.spawn.return_value = SimpleNamespace(id="sa-1")
        dispatcher.subagent_manager = manager
        await dispatcher.handle_message(_forum_msg("/spawn summarise the log"))
        assert "direct message" not in client.sent[-1][0]
        manager.spawn.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "/spawn list",
            "/spawn status",
            "/spawn LIST",
            "/task status",
            # `task_arg_reply` absorbs a leading `run`, so this IS `status` by the
            # time the listing is built -- the scope check has to see it that way.
            "/task run status",
            "/task RUN Status",
        ],
    )
    async def test_the_same_command_is_refused_when_its_ARGUMENT_lists(self, command: str) -> None:
        """Scope follows the argument, which the command name does not show.

        `/spawn list` renders ``manager.running`` -- every subagent on the box, with
        its task text -- and `/task status` reports the one global runner. Neither
        filters on the session, so answering either in a Topic hands host state to
        members who were never allow-listed. Case-insensitively, since the argument
        parser does not normalize and a user types what they type.
        """
        dispatcher, client, _ = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        manager, runner = MagicMock(), MagicMock()
        dispatcher.subagent_manager = manager
        dispatcher.task_runner = runner

        await dispatcher.handle_message(_forum_msg(command))

        assert "direct message" in client.sent[-1][0]
        # Refused BEFORE the listing was built: a reply assembled and then not sent
        # still read host state on behalf of a Topic.
        assert not manager.method_calls and not runner.method_calls

    @pytest.mark.asyncio
    async def test_a_dm_still_gets_the_listing(self) -> None:
        """Non-vacuity: the gate refuses the Topic, not the subcommand."""
        dispatcher, client, _ = _dispatcher({1})
        dispatcher.subagent_manager = SimpleNamespace(running=[], max_concurrent=4)
        await dispatcher.handle_message(_msg("/spawn list"))
        assert "direct message" not in client.sent[-1][0]
        assert "No subagents running." in client.sent[-1][0]


class TestSessionsAuditCaller:
    @pytest.mark.asyncio
    async def test_the_requesting_user_id_is_the_audited_caller(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.history import ConversationLog
        from kiro_crew.messaging import session_resume as resume_core

        dispatcher, _, _ = _dispatcher({7})
        log = ConversationLog(base_dir=tmp_path / "sessions")
        dispatcher.conv_log = log
        dispatcher._session_resume.conv_log = log
        dispatcher._session_resume._controller.conv_log = log
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            resume_core,
            "sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: calls.append(kw)),
        )

        await dispatcher.handle_message(_dm("/session"))

        assert calls and calls[0]["caller"] == "7"
        assert calls[0]["source"] == "telegram"


# ── Dispatch characterization: owner-level contracts a module split must keep ──


class TestAgentPickerCharacterization:
    """The /agent keyboard, its cut, its inert notice row and every reply."""

    _DEFAULT = "Default (kirocrew)"

    @staticmethod
    def _with_names(monkeypatch: Any, d: Any, names: list[str]) -> None:
        monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: list(names)))

    def test_each_internal_signal_hides_an_agent_on_its_own(self) -> None:
        from kiro_crew.telegram.transport_dispatch import _agent_is_internal

        agent = TestAgentPicker._agent
        assert _agent_is_internal(agent("a", "a.json", owned=True)) is True
        assert _agent_is_internal(agent("b", "b.json", source="kirocrew")) is True
        assert _agent_is_internal(agent("c", "app--c.json")) is True
        assert _agent_is_internal(agent("d--x", "d.json")) is False
        assert _agent_is_internal(agent("kirocrew-custom", "kirocrew-custom.json")) is False
        assert _agent_is_internal(agent("p", "Pkg-p.json", source="package")) is False

    def test_names_are_deduplicated_and_listed_with_no_arguments(self) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        roster = [
            TestAgentPicker._agent("beta", "beta.json"),
            TestAgentPicker._agent("alpha", "alpha.json"),
            TestAgentPicker._agent("alpha", "alpha-2.json"),
            TestAgentPicker._agent("", "blank.json"),
        ]
        calls: list[Any] = []
        with patch.object(td, "list_agents", lambda *a, **k: calls.append((a, k)) or roster):
            assert td.TelegramDispatcher._installed_agent_names() == ["alpha", "beta"]
        assert calls == [((), {})]

    def test_discovery_runs_off_the_loop_and_keeps_its_order(self, monkeypatch: Any) -> None:
        import threading

        d, client, _sess = _dispatcher({7})
        threads: list[int] = []

        def _names() -> list[str]:
            threads.append(threading.get_ident())
            return ["zeta", "alpha"]

        monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(_names))
        asyncio.run(d.handle_message(_dm("/agent")))
        assert threads and threads[0] != threading.get_ident()
        labels = [row[0]["text"] for row in client.sent[-1][1]["inline_keyboard"]]
        assert labels == [f"• {self._DEFAULT}", "zeta", "alpha"]

    def test_the_keyboard_header_and_record_are_exact(self, monkeypatch: Any) -> None:
        d, client, _sess = _dispatcher({7})
        self._with_names(monkeypatch, d, ["alpha"])
        asyncio.run(d.handle_message(_dm("/agent")))
        text, markup = client.sent[-1]
        assert text == f"Current agent: {self._DEFAULT}\nPick one:"
        assert markup["inline_keyboard"] == [
            [{"text": f"• {self._DEFAULT}", "callback_data": "g:0"}],
            [{"text": "alpha", "callback_data": "g:1"}],
        ]
        picker = d._agent_pickers["7:101"]
        assert picker.route == ("direct", "7") and picker.session_key == ""
        assert picker.store_route_preference is False
        asyncio.run(d.handle_message(_dm("/agent x")))
        assert client.sent[-1][0] == (
            "/agent takes no argument — pick from the list.\n\n"
            f"Current agent: {self._DEFAULT}\nPick one:"
        )
        d._agent_picks.set(("direct", "7"), "ghost")
        asyncio.run(d.handle_message(_dm("/agent")))
        assert client.sent[-1][0] == "Current agent: ghost\nPick one:"

    def test_the_cut_boundary_and_the_inert_notice(self, monkeypatch: Any) -> None:
        from test_telegram import _press

        d, client, _sess = _dispatcher({7})
        names = [f"agent{i:03d}" for i in range(24)]
        self._with_names(monkeypatch, d, names)
        asyncio.run(d.handle_message(_dm("/agent")))
        rows = client.sent[-1][1]["inline_keyboard"]
        assert len(rows) == 25 and not any("not shown" in r[0]["text"] for r in rows)

        d, client, _sess = _dispatcher({7})
        names = [f"agent{i:03d}" for i in range(25)]
        self._with_names(monkeypatch, d, names)
        asyncio.run(d.handle_message(_dm("/agent")))
        rows = client.sent[-1][1]["inline_keyboard"]
        assert rows[-1] == [{"text": "… and 1 more not shown", "callback_data": "noop"}]
        assert d._agent_pickers["7:101"].choices == (
            (("", self._DEFAULT),) + tuple((n, n) for n in names[:24])
        )
        sent_before = len(client.sent)
        asyncio.run(d.on_callback(_press("noop")))
        assert client.edits == [] and client.markup_edits == [] and len(client.sent) == sent_before
        assert "7:101" in d._agent_pickers
        asyncio.run(d.on_callback(_press("g:25")))
        assert client.edits[-1][1] == "⌛ This agent list is no longer active — send /agent again."

    def test_each_agent_reply_is_exact(self, monkeypatch: Any) -> None:
        from test_telegram import _press

        rotated = (
            "✅ Agent set to {} — this leaves the current conversation. "
            "Switch back to return to it."
        )
        d, client, sess = _dispatcher({7})
        self._with_names(monkeypatch, d, ["alpha"])
        asyncio.run(d.handle_message(_dm("/agent")))
        asyncio.run(d.on_callback(_press("g:1")))
        assert client.edits[-1] == (101, rotated.format("alpha"), {"inline_keyboard": []})
        asyncio.run(d.handle_message(_dm("/agent")))
        asyncio.run(d.on_callback(_press("g:0", message_id=102)))
        assert client.edits[-1][1] == rotated.format(self._DEFAULT)
        assert d._agent_picks.get(("direct", "7")) == ""

        d, client, sess = _dispatcher({7})
        sess._has = False
        assert asyncio.run(d._apply_agent(("direct", "7"), "alpha")) == "✅ Agent set to alpha."
        assert asyncio.run(d._apply_agent(("direct", "7"), "alpha")) == "ℹ️ Already using alpha."
        sess._busy = True
        assert asyncio.run(d._apply_agent(("direct", "7"), "beta")) == (
            "⏳ Still working on your last message — send /agent again once it finishes "
            "to switch to beta."
        )
        # The Default row's label names the configured agent; with no pick it
        # is the row already in use, so each check switches away first.
        d, _client, sess = _dispatcher({7}, default_agent="research")
        sess._has = False
        assert asyncio.run(d._apply_agent(("direct", "7"), "")) == (
            "ℹ️ Already using Default (research)."
        )
        asyncio.run(d._apply_agent(("direct", "7"), "alpha"))
        assert asyncio.run(d._apply_agent(("direct", "7"), "")) == (
            "✅ Agent set to Default (research)."
        )
        d.agent = "override"
        asyncio.run(d._apply_agent(("direct", "7"), "alpha"))
        assert asyncio.run(d._apply_agent(("direct", "7"), "")) == (
            "✅ Agent set to Default (override)."
        )

    def test_a_busy_refusal_is_still_audited_as_allowed(self, monkeypatch: Any) -> None:
        from test_telegram import _press

        rows: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw)),
        )
        d, client, sess = _dispatcher({7})
        self._with_names(monkeypatch, d, ["alpha"])
        asyncio.run(d.handle_message(_dm("/agent")))
        sess._busy = True
        asyncio.run(d.on_callback(_press("g:1")))
        assert [r for r in rows if r["operation"] == "telegram.set_agent"] == [
            {
                "caller": "7",
                "operation": "telegram.set_agent",
                "outcome": "allowed",
                "source": "telegram",
                "resources": "agent=alpha",
            }
        ]
        assert client.edits[-1][1].startswith("⏳ Still working")
        assert client.edits[-1][2] == {"inline_keyboard": []}


class TestServiceAndDashboardCharacterization:
    """The service-backed commands and the dashboard link, reply by reply."""

    @pytest.mark.parametrize(
        ("text", "reply"),
        [
            ("/cron list", "Cron is not running on this instance."),
            ("/spawn x", "Subagents are not available on this instance."),
            ("/task status", "The task runner is not available on this instance."),
        ],
    )
    def test_an_absent_service_says_so(self, text: str, reply: str) -> None:
        d, client, _sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm(text)))
        assert client.sent[-1][0] == reply

    def test_the_usage_replies_are_exact(self) -> None:
        d, client, _sess = _dispatcher({7})
        d.cron_service = MagicMock()
        asyncio.run(d.handle_message(_dm("/cron")))
        assert (
            client.sent[-1][0] == "Usage: /cron list | pause <id> | resume <id> | remove <id>|all"
        )
        d.task_runner = MagicMock()
        asyncio.run(d.handle_message(_dm("/task")))
        assert client.sent[-1][0] == "Usage: /task run <spec-path> | /task status | /task cancel"
        d.subagent_manager = MagicMock()
        asyncio.run(d.handle_message(_dm("/spawn")))
        assert client.sent[-1][0] == "Usage: /spawn <task>  ·  /spawn list"

    @pytest.fixture
    def _dashboard(self, monkeypatch: Any) -> dict[str, Any]:
        import kiro_crew.dashboard.token_auth as token_auth

        state: dict[str, Any] = {"order": [], "minted": []}
        monkeypatch.delenv("KIROCREW_PORT", raising=False)

        def _mint(user: str, *, ttl_seconds: int) -> str:
            state["minted"].append((user, ttl_seconds))
            if state.get("mint_error"):
                raise state["mint_error"]
            return "tok123"

        monkeypatch.setattr(token_auth, "generate_token", _mint)
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: state["order"].append(("sel", kw))),
        )
        return state

    def _dm_dashboard(self, state: dict[str, Any], text: str, url: str) -> Any:
        d, client, _sess = _dispatcher({1})
        d.cfg.dashboard = SimpleNamespace(url=url)
        real_send = client.send_message

        async def _send(chat_id: int, body: str, **kw: Any) -> int:
            state["order"].append(("send", body))
            return await real_send(chat_id, body, **kw)

        client.send_message = _send  # type: ignore[method-assign]
        asyncio.run(d.handle_message(_msg(text)))
        return client

    def test_a_dm_gets_a_link(self, _dashboard: dict[str, Any]) -> None:
        self._dm_dashboard(_dashboard, "/kirocrew dashboard 2h", "https://dash.example:8443")
        assert _dashboard["minted"] == [("1", 7200)]
        assert _dashboard["order"] == [
            (
                "sel",
                {
                    "caller": "1",
                    "operation": "telegram.dashboard_token",
                    "outcome": "ok",
                    "source": "telegram",
                    "resources": "ttl=7200",
                },
            ),
            ("send", "🔗 Dashboard link (valid 2h):\nhttps://dash.example:8443/?token=tok123"),
        ]

    def test_the_ttl_is_clamped_and_rendered(self, _dashboard: dict[str, Any]) -> None:
        for arg, ttl, shown in (("", 3600, "1h"), (" 90m", 5400, "1h 30m"), (" 30h", 72000, "20h")):
            _dashboard["minted"].clear()
            client = self._dm_dashboard(
                _dashboard, f"/kirocrew dashboard{arg}", "https://dash.example"
            )
            assert _dashboard["minted"] == [("1", ttl)], arg
            assert client.sent[-1][0].startswith(f"🔗 Dashboard link (valid {shown}):"), arg

    def test_a_topic_is_refused_without_minting(self, _dashboard: dict[str, Any]) -> None:
        d, client, _sess = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        asyncio.run(d.handle_message(_forum_msg("/kirocrew dashboard")))
        assert client.sent[-1][0] == (
            "🔒 Dashboard links are only sent in a direct message — DM me `/kirocrew dashboard`."
        )
        assert client.send_threads[-1] == 4
        assert _dashboard["minted"] == [] and _dashboard["order"] == []

    def test_a_mint_failure_is_audited_and_reported(self, _dashboard: dict[str, Any]) -> None:
        _dashboard["mint_error"] = RuntimeError("boom")
        client = self._dm_dashboard(_dashboard, "/kirocrew dashboard", "https://dash.example")
        assert [kw["outcome"] for kind, kw in _dashboard["order"] if kind == "sel"] == ["error"]
        assert client.sent[-1][0] == "⚠️ Could not generate dashboard link: boom"


class TestHostListingAudienceCharacterization:
    """The host-wide listings: who may receive them, and what a refusal says."""

    @pytest.mark.asyncio
    async def test_a_topic_refusal_is_exact_and_audited(self, monkeypatch: Any) -> None:
        rows: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw)),
        )
        d, client, _sess = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        await d.handle_message(_forum_msg("/sessions"))
        assert client.sent[-1][0] == (
            "🔒 The conversation list is only sent in a direct message. DM me `/sessions`."
        )
        assert client.send_threads[-1] == 4
        assert rows == [
            {
                "caller": "1",
                "operation": "telegram.sessions_command",
                "outcome": "denied",
                "source": "telegram",
                "resources": "chat=-100999",
                "error": "shared_topic_audience",
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("text", "subject", "cmd"),
        [
            ("/cron pause j1", "scheduled job list", "cron"),
            ("/spawn list", "spawn listing", "spawn"),
            ("/task status", "task listing", "task"),
        ],
    )
    async def test_each_listing_names_its_subject(self, text: str, subject: str, cmd: str) -> None:
        d, client, _sess = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        await d.handle_message(_forum_msg(text))
        assert (
            client.sent[-1][0]
            == f"🔒 The {subject} is only sent in a direct message. DM me `/{cmd}`."
        )

    @pytest.mark.asyncio
    async def test_a_multi_owner_dm_refusal_is_exact_and_audited(self, monkeypatch: Any) -> None:
        rows: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw)),
        )
        d, client, _sess = _dispatcher({7, 8})
        await d.handle_message(_dm("/sessions"))
        assert client.sent[-1][0] == (
            "🔒 The conversation list names conversations across this whole install, so it is "
            "only sent when `telegram.allowed_user_ids` holds a single operator. It currently "
            "holds several, and the agent cannot tell which of them owns the install."
        )
        assert rows == [
            {
                "caller": "7",
                "operation": "telegram.sessions_command",
                "outcome": "denied",
                "source": "telegram",
                "resources": "allowed_identities=2",
                "error": "no_unambiguous_owner",
            }
        ]

    @pytest.mark.asyncio
    async def test_the_picker_gets_its_arguments(self) -> None:
        from unittest.mock import call

        d, client, _sess = _dispatcher({7})
        picker = AsyncMock()
        d._session_resume.show_picker = picker  # type: ignore[method-assign]
        await d.handle_message(_dm("/sessions launch"))
        assert picker.await_args == call(
            client, 7, 7, "private", None, query="launch", native_key="telegram:kirocrew:direct:7"
        )
