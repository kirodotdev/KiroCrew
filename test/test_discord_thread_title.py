"""A Discord thread the bot opened follows its conversation's title.

Three layers: :func:`thread_name` (what a name may say), :func:`rename_bot_thread`
(whose thread may be renamed, once per title, never fatally), and the ledger's
auto-title hand-off (``_record_turn``), which names a conversation after its first
answer the way Telegram does and gives the thread that name.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_test_helpers import _make_state
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import _dispatcher, _inbound

from kiro_crew.dashboard.channel_titles import apply_generated_channel_title_live
from kiro_crew.dashboard.chat_title import _TITLE_ORIGIN_AUTO, _TITLE_ORIGIN_USER
from kiro_crew.discord import thread_title
from kiro_crew.discord import transport_dispatch_ledger as ledger
from kiro_crew.discord.client import DiscordClient, ThreadOrigin
from kiro_crew.discord.client_recheck import SendPermission
from kiro_crew.discord.client_rest import DISCORD_OK, DISCORD_PERMANENT, DiscordApiResult
from kiro_crew.discord.thread_title import rename_bot_thread, thread_name
from kiro_crew.history import ConversationLog
from kiro_crew.messaging import auto_title
from kiro_crew.messaging.dispatch import TurnRecord
from kiro_crew.testing.synthetic_credentials import github_classic

_BOT = "999"
_THREAD = "555"


@pytest.fixture(autouse=True)
def _fresh_title_state() -> Any:
    """Both tables are process globals."""
    auto_title.reset()
    thread_title.reset()
    yield
    auto_title.reset()
    thread_title.reset()


class _Client:
    """The four client members ``rename_bot_thread`` reads."""

    def __init__(self, *, owner: str = _BOT, status: int = 200, permitted: bool = True) -> None:
        self.bot_user_id = _BOT
        self.owner = owner
        self.status = status
        self.permitted = permitted
        self.renames: list[tuple[str, str]] = []
        self.lookups: list[str] = []

    async def thread_origin(self, channel_id: str) -> ThreadOrigin | None:
        self.lookups.append(channel_id)
        return ThreadOrigin(channel_type=11, parent_id="111", owner_id=self.owner)

    async def _destination_still_permitted(self, path: str) -> SendPermission:
        return SendPermission.allow() if self.permitted else SendPermission.revoked()

    async def rename_thread(
        self, thread_id: str, name: str, *, still_current: Any = None
    ) -> DiscordApiResult:
        self.renames.append((thread_id, name))
        self.still_current = still_current
        if self.status == 200:
            return DiscordApiResult(outcome=DISCORD_OK, data={"id": thread_id}, status=200)
        return DiscordApiResult(outcome=DISCORD_PERMANENT, status=self.status, code=50013)


# ── the name ─────────────────────────────────────────────────────────────────


class TestThreadName:
    def test_line_breaks_and_runs_of_space_become_one_space(self) -> None:
        assert thread_name("Fix the\nlogin\r\n\tbug\u2028now") == "Fix the login bug now"

    def test_control_and_format_characters_are_dropped(self) -> None:
        # A bidi override would let a name read as something else in the list.
        assert thread_name("Pay\u202e\u200b\x07 rent") == "Pay rent"
        assert thread_name("Pay\u200brent") == "Pay rent"

    def test_cut_to_discords_limit(self) -> None:
        assert len(thread_name("x" * 250)) == thread_title.THREAD_NAME_MAX == 100

    def test_nothing_printable_is_no_name(self) -> None:
        assert thread_name("\n\u200b\x00 ") == ""

    def test_a_credential_split_by_control_characters_is_never_rejoined(self) -> None:
        # Split by control characters, the token passes a redaction run on the
        # raw title; deleting them would rejoin it, so each becomes a space.
        token = github_classic()
        split = "\x07".join(token[i : i + 4] for i in range(0, len(token), 4))

        name = thread_name(f"Deploy {split}")

        assert token not in name and name.startswith("Deploy ")


# ── the rename ───────────────────────────────────────────────────────────────


class TestRenameBotThread:
    @pytest.mark.asyncio
    async def test_renames_a_thread_the_bot_opened_once_per_title(self) -> None:
        client = _Client()

        assert await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]
        assert not await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]
        assert await rename_bot_thread(client, _THREAD, "Launch plan v2")  # type: ignore[arg-type]

        assert client.renames == [(_THREAD, "Launch plan"), (_THREAD, "Launch plan v2")]

    @pytest.mark.asyncio
    async def test_never_renames_a_thread_someone_else_opened(self) -> None:
        client = _Client(owner="person")

        assert not await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]

        assert client.lookups == [_THREAD] and client.renames == []

    @pytest.mark.asyncio
    async def test_a_withdrawn_destination_is_not_renamed(self) -> None:
        client = _Client(permitted=False)

        assert not await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]

        assert client.renames == []

    @pytest.mark.asyncio
    async def test_a_403_is_quiet_and_not_tried_again_for_that_thread(self) -> None:
        client = _Client(status=403)

        assert not await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]
        assert not await rename_bot_thread(client, _THREAD, "Other name")  # type: ignore[arg-type]

        assert client.renames == [(_THREAD, "Launch plan")]

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_tried_again_on_the_next_title(self) -> None:
        client = _Client(status=429)

        await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]
        await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]

        assert len(client.renames) == 2

    @pytest.mark.asyncio
    async def test_a_stale_name_is_not_written(self) -> None:
        client = _Client()

        stale = await rename_bot_thread(
            client, _THREAD, "Old plan", still_current=lambda: False  # type: ignore[arg-type]
        )

        assert not stale and client.renames == []

    @pytest.mark.asyncio
    async def test_the_staleness_check_rides_into_the_rest_ladder(self) -> None:
        client = _Client()

        def current() -> bool:
            return True

        await rename_bot_thread(client, _THREAD, "Plan", still_current=current)  # type: ignore[arg-type]

        assert client.still_current is current

    @pytest.mark.asyncio
    async def test_a_client_error_never_raises(self) -> None:
        client = _Client()

        async def _boom(_thread_id: str) -> None:
            raise RuntimeError("network down")

        client.thread_origin = _boom  # type: ignore[assignment,method-assign]

        assert not await rename_bot_thread(client, _THREAD, "Launch plan")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_client_verb_patches_only_the_name(self, monkeypatch: Any) -> None:
        client = DiscordClient(token="t")
        calls: list[tuple[Any, ...]] = []

        async def _api_json(method: str, path: str, payload: Any, **kw: Any) -> DiscordApiResult:
            calls.append((method, path, payload))
            return DiscordApiResult(outcome=DISCORD_OK, data={}, status=200)

        monkeypatch.setattr(client, "api_json", _api_json)

        await client.rename_thread(_THREAD, "Launch plan")

        assert calls == [("PATCH", f"/channels/{_THREAD}", {"name": "Launch plan"})]

    @pytest.mark.asyncio
    async def test_the_client_verb_rechecks_staleness_after_a_wait(self, monkeypatch: Any) -> None:
        client = DiscordClient(token="t")
        seen: list[Any] = []

        async def _api_json(method: str, path: str, payload: Any, **kw: Any) -> DiscordApiResult:
            seen.append(kw.get("caller_permitted"))
            return DiscordApiResult(outcome=DISCORD_OK, data={}, status=200)

        monkeypatch.setattr(client, "api_json", _api_json)

        def current() -> bool:
            return True

        await client.rename_thread(_THREAD, "Launch plan", still_current=current)

        assert seen == [current]


# ── the ledger's auto-title hand-off ─────────────────────────────────────────


def _named_by(monkeypatch: Any, title: str = "Launch checklist") -> list[dict[str, Any]]:
    """Replace the naming turn with one that lands *title*, recording its call."""
    calls: list[dict[str, Any]] = []

    async def _fake(sessions: Any, conv_log: Any, key: str, user: str, reply: str, **kw: Any):
        calls.append({"key": key, "user": user, "reply": reply, **kw})
        setter = kw.get("set_channel_title")
        if setter is not None:
            await setter(title)
        return title

    monkeypatch.setattr(auto_title, "maybe_auto_title", _fake)
    return calls


async def _settled() -> None:
    await asyncio.gather(*list(ledger._TITLE_TASKS))


def _wired(tmp_path: Any, *, status: int = 200) -> tuple[Any, _Client, ConversationLog]:
    d, _cli, _sess = _dispatcher({"u1"})
    log = ConversationLog(base_dir=tmp_path / "sessions")
    client = _Client(status=status)
    d.conv_log = log
    d.client = client  # type: ignore[assignment]
    return d, client, log


def _answered(key: str, *, is_new: bool = True) -> TurnRecord:
    return TurnRecord("answered", key, "", "plan the launch", "Here is a plan.", is_new)


class TestLedgerAutoTitle:
    @pytest.mark.asyncio
    async def test_a_new_thread_conversation_is_named_and_its_thread_follows(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        calls = _named_by(monkeypatch)
        d, client, log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)

        await d._record_turn(_answered(key), (), _THREAD)
        await _settled()

        assert [c["key"] for c in calls] == [key] and calls[0]["source"] == "discord"
        assert client.renames == [(_THREAD, "Launch checklist")]
        # No fallback name was written for the naming turn's guard to refuse.
        assert not (log.get_metadata(key) or {}).get("title")

    @pytest.mark.asyncio
    async def test_a_403_leaves_the_turn_recorded_and_the_thread_alone(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        _named_by(monkeypatch)
        d, client, log = _wired(tmp_path, status=403)
        key = d._session_key("u1", _THREAD)

        await d._record_turn(_answered(key), (), _THREAD)
        await _settled()

        assert client.renames == [(_THREAD, "Launch checklist")]
        assert [m["content"] for m in log.read_messages(key)] == [
            "plan the launch",
            "Here is a plan.",
        ]

    @pytest.mark.asyncio
    async def test_a_dm_is_named_without_a_rename(self, monkeypatch: Any, tmp_path: Any) -> None:
        calls = _named_by(monkeypatch)
        d, client, _log = _wired(tmp_path)

        await d._record_turn(_answered(d._session_key("u1")), (), "")
        await _settled()

        # The hook still runs (an open dashboard tab takes the name), with no thread.
        assert len(calls) == 1 and calls[0]["set_channel_title"] is not None
        assert client.renames == []

    @pytest.mark.asyncio
    async def test_a_resumed_session_is_not_named_here(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        calls = _named_by(monkeypatch)
        d, _client, log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)

        await d._record_turn(_answered(key), (), None)
        await _settled()

        assert calls == []
        assert log.get_metadata(key).get("title") == "plan the launch"

    @pytest.mark.asyncio
    async def test_a_conversation_named_before_a_restart_spends_no_naming_turn(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        calls = _named_by(monkeypatch)
        d, _client, log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)
        await d._record_turn(_answered(key), (), None)  # writes the fallback name

        await d._record_turn(_answered(key, is_new=False), (), _THREAD)
        await _settled()

        assert calls == [] and auto_title.is_titled(key)

    @pytest.mark.asyncio
    async def test_a_name_landing_after_new_does_not_rename_the_new_conversation(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        d, client, _log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)

        async def _fake(sessions: Any, conv_log: Any, k: str, *a: Any, **kw: Any) -> str:
            d._conv.bump_gen(f"thread:{_THREAD}")  # `!new` while the name is generated
            await kw["set_channel_title"]("Old plan")
            return "Old plan"

        monkeypatch.setattr(auto_title, "maybe_auto_title", _fake)

        await d._record_turn(_answered(key), (), _THREAD)
        await _settled()

        assert d._session_key("u1", _THREAD) != key
        assert client.renames == []

    @pytest.mark.asyncio
    async def test_a_reacquired_runtime_keeps_the_generated_name(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        # The naming turn wrote the name and kept its claim; a runtime reloaded
        # from the dashboard then reads as a new session on the same key.
        _named_by(monkeypatch)
        d, _client, log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)
        await d._record_turn(_answered(key), (), _THREAD)
        await _settled()
        log.set_title(key, "Launch checklist")
        auto_title.mark_titled(key, auto_title.TITLE_KIND_AUTO)

        await d._record_turn(_answered(key, is_new=True), (), _THREAD)
        await _settled()

        assert log.get_metadata(key).get("title") == "Launch checklist"

    @pytest.mark.asyncio
    async def test_an_empty_first_turn_still_leaves_the_name_to_a_later_answer(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        calls = _named_by(monkeypatch)
        d, client, log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)
        empty = TurnRecord("answered", key, "", "hi", "", True, notice="No reply")

        await d._record_turn(empty, (), _THREAD)
        await _settled()
        assert calls == [] and not (log.get_metadata(key) or {}).get("title")

        await d._record_turn(_answered(key, is_new=False), (), _THREAD)
        await _settled()

        assert [c["key"] for c in calls] == [key]
        assert client.renames == [(_THREAD, "Launch checklist")]


# ── an open dashboard tab takes the generated name ───────────────────────────


def _tab(tmp_path: Any, session_key: str) -> tuple[Any, Any]:
    """A real dashboard state holding one channel-born tab on *session_key*."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.linked_session_key = session_key
    slot.title = "plan the launch"
    slot._titled = True
    slot._title_origin = _TITLE_ORIGIN_AUTO
    return state, slot


class TestLiveTabTitle:
    @pytest.mark.asyncio
    async def test_an_open_tab_takes_the_generated_name_and_stays_auto(self, tmp_path: Any) -> None:
        state, slot = _tab(tmp_path, "discord:kirocrew:direct:u1")
        epoch = slot._title_epoch

        landed = await apply_generated_channel_title_live(
            state, "discord:kirocrew:direct:u1", "Launch checklist"
        )

        assert landed and slot.title == "Launch checklist"
        assert slot._title_origin == _TITLE_ORIGIN_AUTO
        # The epoch bump is what makes the tab's own background titler stand down.
        assert slot._title_epoch == epoch + 1

    @pytest.mark.asyncio
    async def test_a_surfaced_tab_showing_the_fallback_takes_the_name(self, tmp_path: Any) -> None:
        # ``surface_channel_session`` marks the listing's fallback titled and
        # records no origin; that is not a name someone set.
        state, slot = _tab(tmp_path, "discord:kirocrew:direct:u1")
        slot._title_origin = ""

        landed = await apply_generated_channel_title_live(
            state, "discord:kirocrew:direct:u1", "Launch checklist"
        )

        assert landed and slot.title == "Launch checklist"
        assert slot._title_origin == _TITLE_ORIGIN_AUTO

    @pytest.mark.asyncio
    async def test_a_tab_whose_transcript_is_being_deleted_is_left_alone(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        # A permanent delete unlinks the transcript before it removes the slot;
        # a title written in that window would recreate it as a title-only file.
        state, slot = _tab(tmp_path, "discord:kirocrew:direct:u1")
        monkeypatch.setattr(state.conversation_log, "delete_in_flight", lambda _key: True)

        landed = await apply_generated_channel_title_live(
            state, "discord:kirocrew:direct:u1", "Launch checklist"
        )

        assert not landed and slot.title == "plan the launch"

    @pytest.mark.asyncio
    async def test_a_delete_starting_during_the_write_recreates_no_transcript(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        # The delete begins after the pre-check passed: only the re-check
        # inside the write lock can refuse the metadata write.
        key = "discord:kirocrew:direct:u1"
        state, _slot = _tab(tmp_path, key)
        reads: list[str] = []

        def _in_flight(history_key: str) -> bool:
            reads.append(history_key)
            return len(reads) > 1

        monkeypatch.setattr(state.conversation_log, "delete_in_flight", _in_flight)

        await apply_generated_channel_title_live(state, key, "Launch checklist")

        assert len(reads) > 1
        assert not state.conversation_log.get_metadata(key)

    @pytest.mark.asyncio
    async def test_a_hand_set_tab_name_is_kept(self, tmp_path: Any) -> None:
        state, slot = _tab(tmp_path, "discord:kirocrew:direct:u1")
        slot.title = "My launch"
        slot._title_origin = _TITLE_ORIGIN_USER

        landed = await apply_generated_channel_title_live(
            state, "discord:kirocrew:direct:u1", "Launch checklist"
        )

        assert not landed and slot.title == "My launch"
        assert slot._title_origin == _TITLE_ORIGIN_USER

    @pytest.mark.asyncio
    async def test_no_open_tab_changes_nothing(self, tmp_path: Any) -> None:
        state, slot = _tab(tmp_path, "discord:kirocrew:direct:u1")

        landed = await apply_generated_channel_title_live(
            state, "discord:kirocrew:direct:other", "Launch checklist"
        )

        assert not landed and slot.title == "plan the launch"

    @pytest.mark.asyncio
    async def test_the_naming_hook_names_the_open_tab_for_a_dm(self, tmp_path: Any) -> None:
        d, client, _log = _wired(tmp_path)
        key = d._session_key("u1")
        state, slot = _tab(tmp_path, key)
        d._session_resume.dashboard_state = state

        await ledger._title_follows(d, key, "")("Launch checklist")

        assert slot.title == "Launch checklist" and slot._title_origin == _TITLE_ORIGIN_AUTO
        assert client.renames == []

    @pytest.mark.asyncio
    async def test_the_naming_hook_names_the_open_tab_and_the_thread(self, tmp_path: Any) -> None:
        d, client, _log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)
        state, slot = _tab(tmp_path, key)
        d._session_resume.dashboard_state = state

        await ledger._title_follows(d, key, _THREAD)("Launch checklist")

        assert slot.title == "Launch checklist"
        assert client.renames == [(_THREAD, "Launch checklist")]

    @pytest.mark.asyncio
    async def test_a_failing_tab_sync_still_renames_the_thread(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        from kiro_crew.dashboard import channel_titles

        async def _boom(*_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("dashboard down")

        monkeypatch.setattr(channel_titles, "apply_generated_channel_title_live", _boom)
        d, client, _log = _wired(tmp_path)
        key = d._session_key("u1", _THREAD)

        await ledger._title_follows(d, key, _THREAD)("Launch checklist")

        assert client.renames == [(_THREAD, "Launch checklist")]


class TestHandleMessageWiring:
    @pytest.mark.asyncio
    async def test_a_thread_turn_hands_its_thread_to_the_ledger(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"}, allowed_threads={_THREAD})
        seen: list[Any] = []

        async def _record(record: TurnRecord, images: Any = (), title_thread: Any = None) -> None:
            seen.append(title_thread)

        d._record_turn = _record  # type: ignore[method-assign]

        await d.handle_message(_inbound("plan the launch", thread_id=_THREAD))
        await d.handle_message(_inbound("plan the launch"))

        assert seen == [_THREAD, ""]
