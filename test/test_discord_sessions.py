"""Discord ``!sessions``: list, search, pick and continue a dashboard session in a DM.

This file holds the listing and search, ``!new`` generations, the pick and its rollback,
the resumed turn's projection and persistence, the replay preview and the new-session
bookkeeping. Where a turn or a button press in a resumed DM goes is in
``test_discord_sessions_routing.py``; the binding under concurrent rebinds, a binding
lost under the conversation and failed writes is in
``test_discord_sessions_binding_state.py``. The fakes the three share are in
``discord_sessions_helpers.py``.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import discord_sessions_helpers
import pytest
from discord_sessions_helpers import (
    _Client,
    _ConversationLog,
    _dispatcher,
    _interaction,
    _log,
    _log_with_titles,
    _message,
    _picker_button,
)

from kiro_crew import history_search
from kiro_crew.discord import session_resume
from kiro_crew.discord.client import DISCORD_CHUNK_LIMIT
from kiro_crew.discord.commands import parse_command, parse_command_argument
from kiro_crew.history import transcript_stem
from kiro_crew.messaging import session_resume as resume_core
from kiro_crew.messaging.link import ChannelLink

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = discord_sessions_helpers._isolated_home

# test_discord_resumed_busy.py imports this from here.
_bind_to_chat1 = discord_sessions_helpers._bind_to_chat1


def _picker_labels(client: _Client) -> list[str]:
    _, components = client.sent[-1]
    return [button["label"] for row in components for button in row["components"]]


@pytest.mark.asyncio
async def test_sessions_finds_a_session_by_conversation_content() -> None:
    """The original bug: searching a phrase from the CONVERSATION, not the title.

    A picker calling ``list_sessions`` and filtering on titles only means a
    query the user remembered from the discussion could never match. Routing
    through the shared ``search_sessions`` -- the same one the dashboard uses --
    makes message content searchable.
    """
    log = _ConversationLog(
        [
            {"key": "dashboard_chat-0", "title": "Untitled", "memory_mode": "persistent"},
            {"key": "dashboard_chat-1", "title": "Also untitled", "memory_mode": "persistent"},
        ],
        {
            "dashboard:chat-0": [{"role": "user", "content": "unrelated chatter"}],
            "dashboard:chat-1": [
                {"role": "user", "content": "how do I link to a specific session?"}
            ],
        },
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session link to a specific session"))

    # Matched on content despite neither title containing the phrase.
    assert _picker_labels(client) == ["1. Also untitled"]


@pytest.mark.asyncio
async def test_sessions_cjk_query_reaches_title_fallback() -> None:
    """A spaceless CJK query must trigger the zero-hit TITLE fallback.

    The fallback must not gate on a whitespace word count, which a spaceless
    CJK query (one "word") never satisfied — so when the shared search found
    nothing, the fallback silently demanded the literal title substring. The
    gate now derives from the same parse_search_query needles as the shared
    search, so a title holding the query's words apart still resolves.
    """
    log = _ConversationLog(
        [
            {"key": "dashboard_chat-0", "title": "内存的泄漏问题排查", "memory_mode": "persistent"},
            {"key": "dashboard_chat-1", "title": "Unrelated", "memory_mode": "persistent"},
        ],
        {"dashboard:chat-0": [], "dashboard:chat-1": []},
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session 内存泄漏"))

    # The fake shared search misses (no literal phrase anywhere), so only the
    # title fallback — running the REAL parse + gate — can produce this row.
    assert _picker_labels(client) == ["1. 内存的泄漏问题排查"]


class _OutsideTheWindowLog(_ConversationLog):
    """A shared search that finds nothing, as for a session older than its scan window."""

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        self.search_calls.append((query, limit))
        return []


@pytest.mark.asyncio
async def test_a_one_word_chinese_query_keeps_the_title_fallback() -> None:
    """With the dictionary, "内存" is ONE required word, so the needles equal the phrase.

    Its characters always made it multi-word, which is what opens the title fallback
    for a session past the shared search's window; the split must not close it.
    """
    history_search.register_cjk_segmenter(lambda runs: [["内存"] for _ in runs])
    log = _OutsideTheWindowLog(
        [{"key": "dashboard_chat-0", "title": "内存占用排查", "memory_mode": "persistent"}],
        {"dashboard:chat-0": []},
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session 内存"))

    assert log.search_calls, "the shared search ran first"
    assert _picker_labels(client) == ["1. 内存占用排查"]


@pytest.mark.asyncio
async def test_the_resume_parse_runs_off_the_event_loop(monkeypatch) -> None:
    """The parse may load the Chinese dictionary on first use; the loop must not pay for it."""
    seen: list[bool] = []
    real = resume_core.parse_search_query

    def parse(query: str):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(query)

    monkeypatch.setattr(resume_core, "parse_search_query", parse)
    log = _OutsideTheWindowLog(
        [{"key": "dashboard_chat-0", "title": "内存占用排查", "memory_mode": "persistent"}],
        {"dashboard:chat-0": []},
    )

    await resume_core.resolve_session_choices(log, "内存", lambda text, _limit: text)

    assert seen == [False]


@pytest.mark.asyncio
async def test_sessions_delegates_to_the_shared_search() -> None:
    """Assert DELEGATION, not re-implemented ranking.

    The scoring formula belongs to KiroCrewHistory.search_sessions and is tested
    there; what this surface must guarantee is that it calls that search rather
    than growing a second one that drifts from the dashboard.
    """
    log = _log_with_titles("Codex compaction investigation")
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions codex"))

    assert log.search_calls, "picker did not call search_sessions"
    query, limit = log.search_calls[0]
    assert query == "codex"
    assert limit > 1, "must fetch more rows than one so filtering cannot starve the picker"


@pytest.mark.asyncio
async def test_sessions_empty_query_does_not_search() -> None:
    """A bare `!sessions` is a listing, not a search -- no query, no search call."""
    log = _log_with_titles("One", "Two")
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions"))

    assert log.search_calls == []
    assert log.list_calls >= 1
    assert len(_picker_labels(client)) == 2


def test_sessions_command_aliases() -> None:
    assert parse_command("!sessions") == "sessions"
    assert parse_command("/sessions") == "sessions"
    assert parse_command("!session Link to a specific session") == "sessions"
    assert parse_command_argument("!session Link to a specific session") == (
        "Link to a specific session"
    )
    assert parse_command_argument("!sessions") == ""


@pytest.mark.asyncio
async def test_sessions_keyword_filters_beyond_recent_limit() -> None:
    log = _log_with_titles(
        *(f"Routine session {index}" for index in range(12)),
        "Codex compaction investigation",
    )
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!session codex"))

    text, _ = client.sent[-1]
    assert _picker_labels(client) == ["1. Codex compaction investigation"]
    assert "Session search" in text
    assert "for `codex`" in text


@pytest.mark.asyncio
async def test_sessions_multi_word_query_matches_case_insensitively() -> None:
    log = _log_with_titles("Other work", "Link to a Specific Session")
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions specific link"))

    assert _picker_labels(client) == ["1. Link to a Specific Session"]


@pytest.mark.asyncio
async def test_sessions_no_match_is_explicit() -> None:
    dispatcher, client, _ = _dispatcher({"u1"}, _log())

    await dispatcher.handle_message(_message("!sessions missing topic"))

    text, components = client.sent[-1]
    assert components is None
    assert "No sessions matched `missing topic`" in text
    assert "Try fewer words" in text
    assert "`!sessions`" in text
    assert len(dispatcher._session_pickers) == 0


@pytest.mark.asyncio
async def test_empty_sessions_query_keeps_recent_order_and_discloses_cap() -> None:
    log = _log_with_titles(*(f"Recent session {index}" for index in range(12)))
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions   "))

    assert _picker_labels(client) == [f"{index + 1}. Recent session {index}" for index in range(10)]
    assert "Showing 10 of 12 most recent sessions" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_sessions_search_cap_is_enforced_and_disclosed() -> None:
    log = _log_with_titles(*(f"Codex session {index}" for index in range(12)))
    dispatcher, client, _ = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!sessions codex"))

    assert len(_picker_labels(client)) == 10
    assert "Showing 10 of 12 matching sessions" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_sessions_requires_exactly_one_allowed_user() -> None:
    log = _log()
    dispatcher, client, _ = _dispatcher({"u1", "u2"}, log)

    await dispatcher.handle_message(_message("!sessions private"))

    assert log.list_calls == 0
    assert "exactly one" in client.sent[-1][0]
    assert client.sent[-1][1] is None


@pytest.mark.asyncio
async def test_sessions_lists_dashboard_and_same_dm_generations_only_and_redacts() -> None:
    secret = "ghp_" + "a" * 36
    prior_key = "discord:kirocrew:direct:u1:gen4"
    other_user_key = "discord:kirocrew:direct:u2:gen4"
    log = _ConversationLog(
        [
            {
                "key": "dashboard_chat-1",
                "title": f"Deploy with {secret}",
                "memory_mode": "persistent",
            },
            {
                "key": "discord_kirocrew_direct_u1",
                "title": "Current Discord session",
                "memory_mode": "persistent",
            },
            {
                "key": transcript_stem(prior_key),
                "title": "Earlier Discord generation",
                "memory_mode": "persistent",
            },
            {
                "key": transcript_stem(other_user_key),
                "title": "Another user's Discord session",
                "memory_mode": "persistent",
            },
            {
                "key": "dashboard_private",
                "title": "Incognito",
                "memory_mode": "temporary",
            },
        ],
        {
            "dashboard:chat-1": [],
            "discord:kirocrew:direct:u1": [],
            prior_key: [],
            other_user_key: [],
        },
    )
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    sessions.channel_keys.update({dispatcher.current_session_key("u1"), prior_key, other_user_key})

    await dispatcher.handle_message(_message("!sessions"))

    text, components = client.sent[-1]
    buttons = [button for row in components for button in row["components"]]
    assert "Recent sessions" in text
    labels = [button["label"] for button in buttons]
    assert labels[0].startswith("1. Deploy with [REDACTED")
    assert labels[1:] == [
        "2. Current Discord session",
        "3. Earlier Discord generation",
    ]
    assert buttons[0]["custom_id"].startswith("s:")
    assert secret not in buttons[0]["label"]
    assert "REDACTED" in buttons[0]["label"]


@pytest.mark.asyncio
async def test_repeated_new_does_not_materialize_empty_history_rows() -> None:
    log = _log()
    dispatcher, _, sessions = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!new"))
    first_key = dispatcher.current_session_key("u1")
    await dispatcher.handle_message(_message("!new"))
    second_key = dispatcher.current_session_key("u1")

    assert first_key != second_key
    assert not log.has_log(first_key)
    assert not log.has_log(second_key)
    assert {first_key, second_key} <= sessions.reserved_generations


@pytest.mark.asyncio
async def test_new_generation_survives_restart_before_its_first_turn() -> None:
    log = _log()
    dispatcher, _, sessions = _dispatcher({"u1"}, log)

    await dispatcher.handle_message(_message("!new"))
    new_key = dispatcher.current_session_key("u1")

    restarted, _, _ = _dispatcher({"u1"}, log, sessions=sessions)
    assert restarted.current_session_key("u1") == new_key


@pytest.mark.asyncio
async def test_new_reports_generation_floor_failure_without_rolling_back() -> None:
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    old_key = dispatcher.current_session_key("u1")
    sessions.reserve_error = OSError("disk unavailable")

    await dispatcher.handle_message(_message("!new"))

    assert dispatcher.current_session_key("u1") != old_key
    assert "could not be saved for restart" in client.sent[-1][0]


@pytest.mark.asyncio
async def test_native_generation_pick_replaces_only_same_dm_origin_mirrors() -> None:
    prior_key = "discord:kirocrew:direct:u1:gen4"
    log = _ConversationLog(
        [
            {
                "key": transcript_stem(prior_key),
                "title": "Earlier Discord generation",
                "memory_mode": "persistent",
            }
        ],
        {prior_key: []},
    )
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    current_key = dispatcher.current_session_key("u1")
    sessions.channel_keys.update({current_key, prior_key})
    link = ChannelLink(channel_type="discord", channel_id="c1")
    sessions.set_mirror_link(current_key, link)
    sessions.set_mirror_link(prior_key, link)

    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert sessions.find_mirror_sessions(link, inbound_only=True) == [prior_key]
    assert current_key not in sessions.mirror_links


@pytest.mark.asyncio
async def test_a_failed_pick_restores_displaced_rows_without_laundering_them() -> None:
    """The pick commit clears the same-DM origin mirrors it displaces; when the
    commit then fails, the rollback puts each displaced row back as it WAS -- a
    genuine admission byte-for-byte, a forged one stripped to the location -- and
    never re-signs anything, so a rollback cannot turn a planted row into a trusted
    one. The pick's own signed link is not left behind either.
    """
    import dataclasses

    from kiro_crew.mirror_admission import sign_mirror_admission, verify_mirror_admission

    prior_key = "discord:kirocrew:direct:u1:gen4"
    log = _ConversationLog(
        [
            {
                "key": transcript_stem(prior_key),
                "title": "Earlier Discord generation",
                "memory_mode": "persistent",
            }
        ],
        {prior_key: []},
    )
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    current_key = dispatcher.current_session_key("u1")
    sessions.channel_keys.update({current_key, prior_key})
    location = ChannelLink(channel_type="discord", channel_id="c1")
    genuine_row = dataclasses.replace(
        location,
        principal="u1",
        admission=sign_mirror_admission(current_key, dataclasses.replace(location, principal="u1")),
    )
    forged_row = dataclasses.replace(location, principal="u1", admission="f" * 64)
    sessions.set_mirror_link(current_key, genuine_row)
    sessions.set_mirror_link(prior_key, forged_row)

    real_set = sessions.set_mirror_link
    calls: list[str] = []

    def _fail_the_claim(key, link, **kwargs):
        calls.append(key)
        if key == prior_key and kwargs.get("accepts_inbound"):
            raise OSError("map write failed")
        return real_set(key, link, **kwargs)

    sessions.set_mirror_link = _fail_the_claim  # type: ignore[method-assign]
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    # The displaced current-generation row is back exactly as it was, still verifying.
    restored = sessions.mirror_links[current_key]
    assert restored == location and restored.admission == genuine_row.admission
    assert verify_mirror_admission(current_key, restored) is True
    # The forged row on the chosen key was never written over, and nothing signed
    # it on the way: it is exactly as planted and still does not verify.
    prior = sessions.mirror_links[prior_key]
    assert prior == location and prior.admission == "f" * 64
    assert verify_mirror_admission(prior_key, prior) is False
    assert prior_key not in sessions.inbound_keys
    assert prior_key in calls, "the commit never reached the failing claim"


@pytest.mark.asyncio
async def test_a_failed_picks_rollback_leaves_a_refreshed_relink_in_place(monkeypatch) -> None:
    """The pick claims its row, then the commit fails on the way out; between that
    failure and the rollback's own critical section the signing key rotates and the
    dashboard re-links the same session to the same DM under a fresh admission. The
    rollback owns only the row it stored. A location match would take the refreshed
    row for its own, clear it and leave the session unbound (or restore an obsolete
    row); whole-row identity leaves it alone and it still verifies under the new key.
    """
    import dataclasses
    import secrets

    from kiro_crew.dashboard import token_secret
    from kiro_crew.mirror_admission import sign_mirror_admission, verify_mirror_admission

    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    location = ChannelLink(channel_type="discord", channel_id="c1")
    rotated = secrets.token_bytes(32)
    refreshed: dict[str, ChannelLink] = {}
    sections = {"n": 0}

    @contextmanager
    def _fail_the_commit_then_relink():
        sections["n"] += 1
        if sections["n"] == 1:
            yield  # the commit's writes land ...
            raise OSError("map flush failed")  # ... and the section fails on the way out
        if sections["n"] == 2:
            # Between the two critical sections: rotation, then the re-link.
            monkeypatch.setattr(token_secret, "_get_secret", lambda: rotated)
            row = dataclasses.replace(location, principal="u1")
            row = dataclasses.replace(row, admission=sign_mirror_admission("dashboard:chat-1", row))
            sessions.mirror_links["dashboard:chat-1"] = row
            sessions.inbound_keys.add("dashboard:chat-1")
            refreshed["row"] = row
        yield

    sessions.batched_save = _fail_the_commit_then_relink  # type: ignore[method-assign]
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert sections["n"] >= 2, "the rollback never ran"
    current = sessions.mirror_links.get("dashboard:chat-1")
    assert current is not None, "the stale rollback cleared the refreshed binding"
    assert current == location and current.admission == refreshed["row"].admission
    assert verify_mirror_admission("dashboard:chat-1", current) is True
    assert "dashboard:chat-1" in sessions.inbound_keys


@pytest.mark.asyncio
async def test_binding_claimed_during_header_edit_is_not_overwritten() -> None:
    """A link that lands while the header edit is in flight must win.

    `_bind_lock` only serialises Discord's own picker. A dashboard mirror POST
    or another channel's `!link` takes no such lock, so the conflict checks are
    re-run after the awaited edit; without that, this PR's own double-binding
    rules would be bypassed and the newer binding silently replaced.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    rival = ChannelLink(channel_type="telegram", channel_id="rival")

    async def _claim_mid_edit(*args: Any, **kwargs: Any) -> bool:
        # Simulates the dashboard/other-channel bind landing during the
        # Discord round-trip the real edit_message performs.
        sessions.set_mirror_link("dashboard:chat-1", rival)
        return True

    client.edit_message = _claim_mid_edit  # type: ignore[assignment]

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    # The rival binding survives, and the resume did NOT mark it inbound.
    assert sessions.mirror_links["dashboard:chat-1"] == rival
    assert "dashboard:chat-1" not in sessions.inbound_keys


@pytest.mark.asyncio
async def test_resumed_turn_lands_in_live_dashboard_window() -> None:
    """A resumed turn is projected user-first without a new user-row broadcast."""
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    durable: list[tuple[str, str, str]] = []

    def _append_if_absent(
        key: str,
        role: str,
        content: str,
        *,
        agent: str | None = None,
        mid: str | None = None,
    ) -> None:
        durable.append((role, content, mid or ""))

    log.append_if_absent = _append_if_absent  # type: ignore[attr-defined]

    class _Slot:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []
            self.broadcasts: list[tuple[str, bool]] = []
            self.is_restricted = False

        def append(self, role: str, content: str, cls: str = "", **kw: Any) -> dict[str, Any]:
            mid = f"m-{len(self.messages) + 1:016x}"
            row = {"role": role, "content": content, "meta": {"mid": mid}}
            self.messages.append(row)
            self.broadcasts.append((role, bool(kw.get("broadcast_user"))))
            return row

    class _State:
        def __init__(self) -> None:
            self.slot = _Slot()
            self.pushes = 0

        def get_slot(self, name: str) -> Any:
            return self.slot if name == "chat-1" else None

        def push_slots_update(self) -> None:
            self.pushes += 1

    state = _State()
    dispatcher._session_resume.dashboard_state = state

    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    pushes_before_turn = state.pushes
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    assert [(row["role"], row["content"]) for row in state.slot.messages] == [
        ("user", "continue here"),
        ("assistant", "Answer: continue here"),
    ]
    assert state.slot.broadcasts == [("user", False), ("assistant", False)]
    assert durable == [
        ("user", "continue here", "m-0000000000000001"),
        ("assistant", "Answer: continue here", "m-0000000000000002"),
    ]
    assert state.pushes == pushes_before_turn + 1


@pytest.mark.asyncio
async def test_restricted_resumed_turn_is_neither_projected_nor_persisted() -> None:
    """Discord had the same two-writer leak as Telegram.

    A resumed ``dashboard:`` key gets its privacy mode from the dashboard slot.
    Without the caller-side gate Discord projected the turn into that slot (making
    it dirty for a later flush) and also appended it directly to durable history.
    Neither path may see content for an incognito or temporary session.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    durable_before = list(log.messages["dashboard:chat-1"])
    persist_calls: list[str] = []
    real_persist = dispatcher._persist_turn

    def _persist(*args: Any, **kwargs: Any) -> None:
        persist_calls.append(str(args[0]))
        real_persist(*args, **kwargs)

    dispatcher._persist_turn = _persist  # type: ignore[method-assign]

    class _RestrictedSlot:
        is_restricted = True

        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        def append(self, role: str, content: str, cls: str = "", **kw: Any) -> dict[str, Any]:
            row = {"role": role, "content": content, "meta": {"mid": f"m-{len(self.messages)}"}}
            self.messages.append(row)
            return row

    class _State:
        def __init__(self) -> None:
            self.slot = _RestrictedSlot()

        def get_slot(self, name: str) -> Any:
            return self.slot if name == "chat-1" else None

        def push_slots_update(self) -> None:
            raise AssertionError("a restricted turn must not dirty or push its slot")

    state = _State()
    dispatcher._session_resume.dashboard_state = state

    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    await dispatcher.handle_message(_message("private continuation"))

    assert sessions.last_key == "dashboard:chat-1"
    assert state.slot.messages == []
    assert persist_calls == []
    assert log.messages["dashboard:chat-1"] == durable_before


@pytest.mark.asyncio
async def test_discord_restricted_decision_allows_unknown_but_denies_live_slot() -> None:
    """Cold ordinary resumes keep history; an affirmative live restriction wins."""
    dispatcher, _, _ = _dispatcher({"u1"}, _log())

    dispatcher._session_resume.dashboard_state = SimpleNamespace(
        sessions=None, get_slot=lambda _name: SimpleNamespace(is_restricted=True)
    )
    assert await dispatcher._session_restricted("dashboard:chat-1") is True

    dispatcher._session_resume.dashboard_state = SimpleNamespace(
        sessions=None, get_slot=lambda _name: None
    )
    assert await dispatcher._session_restricted("dashboard:missing") is False


@pytest.mark.asyncio
async def test_mirrored_turn_persists_idempotently() -> None:
    """The slot's own save re-serializes its window, so the disk write must not
    duplicate what the live slot already carries."""
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)

    calls: list[str] = []
    log.append_if_absent = lambda *a, **k: calls.append("if_absent")  # type: ignore[attr-defined]

    class _State:
        def get_slot(self, name: str) -> Any:
            return type(
                "S",
                (),
                {"append": lambda *a, **k: None, "is_restricted": False},
            )()

        def push_slots_update(self) -> None:
            return None

    dispatcher._session_resume.dashboard_state = _State()
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    before = len(log.messages["dashboard:chat-1"])

    await dispatcher.handle_message(_message("continue here"))

    # Idempotent path used, and no plain append duplicated the turn on disk.
    assert calls, "expected append_if_absent when a live slot took the turn"
    assert len(log.messages["dashboard:chat-1"]) == before


@pytest.mark.asyncio
async def test_resumed_turn_without_live_slot_still_persists() -> None:
    """No open slot (the common phone-only case) → plain disk append."""
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    before = len(log.messages["dashboard:chat-1"])

    await dispatcher.handle_message(_message("continue here"))

    assert len(log.messages["dashboard:chat-1"]) > before


@pytest.mark.asyncio
async def test_stacked_dashboard_prefix_binds_canonical_session() -> None:
    log = _ConversationLog(
        [
            {
                "key": "dashboard_dashboard_chat-1",
                "title": "Launch plan",
                "memory_mode": "persistent",
            }
        ],
        {"dashboard:chat-1": [{"role": "assistant", "content": "prior work"}]},
    )
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    assert set(sessions.mirror_links) == {"dashboard:chat-1"}
    assert sessions.inbound_keys == {"dashboard:chat-1"}


@pytest.mark.asyncio
async def test_choice_binds_replays_and_routes_followup() -> None:
    secret = "ghp_" + "b" * 36
    messages = [
        {"role": "user", "content": "omitted oldest"},
        {"role": "assistant", "content": "context one"},
        {"role": "user", "content": f"credential {secret}"},
        {"role": "assistant", "content": "context three"},
        {"role": "user", "content": "@everyone context four"},
        {"role": "assistant", "content": "context five"},
    ]
    log = _log()
    log.messages["dashboard:chat-1"] = messages
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    link = ChannelLink(channel_type="discord", channel_id="c1")
    assert sessions.mirror_links["dashboard:chat-1"] == link
    assert "dashboard:chat-1" in sessions.inbound_keys
    visible = "\n".join([text for text, _ in client.sent] + [text for _, text, _ in client.edits])
    assert "Resumed: Launch plan" in visible
    assert "omitted oldest" not in visible
    assert secret not in visible
    assert "@everyone" not in visible

    await dispatcher.handle_message(_message("continue here"))
    assert sessions.last_key == "dashboard:chat-1"


@pytest.mark.asyncio
async def test_choice_records_the_owner_as_the_bindings_peer() -> None:
    """A ``!sessions`` pick writes WHO the DM was admitted for beside WHERE it is.

    The resumed dashboard session's key names no principal and a Discord DM
    channel id cannot be tested against the user roster, so without this record the
    per-send recipient check refused every reply the dashboard side produced into
    this DM. The peer is the pressing owner -- the only human a picker press in a
    DM can come from -- and it is not part of the binding's identity, so the
    location match the dispatcher routes inbound by is unchanged.
    """
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    bound = sessions.mirror_links["dashboard:chat-1"]
    assert bound.principal == "u1"
    assert bound == ChannelLink(channel_type="discord", channel_id="c1")
    assert dispatcher._session_resume.resumed_session("c1") == "dashboard:chat-1"


@pytest.mark.parametrize("banner_lands", [True, False], ids=["banner-lands", "banner-lost"])
@pytest.mark.asyncio
async def test_resume_evidence_is_durable_before_the_success_banner(banner_lands) -> None:
    dispatcher, client, sessions = _dispatcher({"u1"}, _log())
    resume = dispatcher._session_resume
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    real_edit, seen = client.edit_message, []

    async def _crash_boundary_edit(channel_id, message_id, text, *, components=None):
        # The gateway may die as Discord commits this edit; only durable state survives.
        if "Resumed" in text:
            seen.append(await resume._expectations.get("c1"))
            if not banner_lands:
                return False
        return await real_edit(channel_id, message_id, text, components=components)

    client.edit_message = _crash_boundary_edit  # type: ignore[method-assign]
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert seen and seen[0] is not None, "success banner shown before evidence was durable"
    assert ("dashboard:chat-1" in sessions.inbound_keys) is banner_lands
    assert await resume._expectations.get("c1") is not None, "a lost banner erased the evidence"


@pytest.mark.asyncio
async def test_resume_replay_sanitizes_internal_protocol() -> None:
    log = _log()
    log.messages["dashboard:chat-1"] = [
        {"role": "user", "content": "Conversation compacted: real question"},
        {
            "role": "assistant",
            "content": "✅ Conversation compacted: ## OBJECTIVE\ninternal guidance",
            "meta": {"kind": "compaction"},
        },
        {
            "role": "assistant",
            "content": "Conversation compacted: ## USER GUIDANCE\nlegacy internal body",
        },
        {
            "role": "assistant",
            "content": (
                "before [STEERING steer-7e6a4a0d-9431-4d2d-b000-000000000001: "
                "internal steer] after"
            ),
        },
        {"role": "assistant", "content": "real answer"},
    ]
    dispatcher, client, _ = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    visible = " ".join(text for text, _ in client.sent)
    assert "Conversation compacted: real question" in visible
    assert "real question" in visible and "real answer" in visible
    assert "before" in visible and "after" in visible
    assert "OBJECTIVE" not in visible and "USER GUIDANCE" not in visible
    assert "internal guidance" not in visible and "legacy internal body" not in visible
    assert "STEERING" not in visible and "internal steer" not in visible


@pytest.mark.asyncio
async def test_resume_replay_previews_long_markdown_without_breaking_a_fence() -> None:
    """A replayed transcript entry is shortened at a boundary the grammar accepts.

    A blind character slice lands inside the fenced code block and Discord then
    renders everything after it as literal backticks instead of code, so the one
    message meant to give the user context arrives unreadable. Taking the shared
    splitter's FIRST chunk shortens at a boundary instead: every chunk but the
    last is sealed, so the block the cut opened is closed.
    """
    body = "```python\n" + "x = 1\n" * 800 + "```\n"
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": body}]
    dispatcher, client, _ = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    replayed = [text for text, _ in client.sent if "x = 1" in text]
    assert len(replayed) == 1, replayed  # still ONE message per transcript entry
    preview = replayed[0]
    assert len(preview) <= DISCORD_CHUNK_LIMIT, len(preview)
    assert preview.count("```") % 2 == 0, preview[-40:]  # the block is closed
    assert preview.endswith(session_resume._REPLAY_TRUNCATED)  # and says so
    # The icon sits on its own line, so the opener still starts a line.
    assert preview.startswith("🤖\n```python\n")


@pytest.mark.asyncio
async def test_resume_replay_omits_a_fence_that_cannot_fit_safely() -> None:
    """An extreme opener cannot fit together with its synthetic closer.

    Sending that sealed chunk anyway lets Discord's client-side cap remove the
    closer and truncation marker. The replay keeps the canonical transcript
    intact and emits only the explicit marker for this pathological preview.
    """
    opener = "`" * 1001
    body = opener + "\n" + ("x" * 1800) + "\n"
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": body}]
    dispatcher, client, _ = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)

    await dispatcher.on_interaction(_interaction(custom_id, message_id))

    replayed = [text for text, _ in client.sent if session_resume._REPLAY_TRUNCATED in text]
    assert replayed == [f"🤖\n{session_resume._REPLAY_TRUNCATED}"]
    assert len(replayed[0]) <= DISCORD_CHUNK_LIMIT
    assert opener not in replayed[0]


@pytest.mark.asyncio
async def test_resume_replay_bounds_and_offloads_the_splitter_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript-sized pathological line is bounded and split off-loop."""
    probes: list[str] = []
    offloads: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def _capture(probe: str, limit: int, *, reserve: int = 0, redactor: Any = None) -> list[str]:
        probes.append(probe)
        return ["safe preview"]

    async def _offload(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloads.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(session_resume, "split_markdown_safe", _capture)
    monkeypatch.setattr(session_resume.asyncio, "to_thread", _offload)

    preview = await session_resume._replay_preview(
        "`" * 1_000_000,
        session_resume._REPLAY_TEXT_LIMIT,
        reserve=session_resume._REPLAY_RESERVE,
    )

    assert len(probes) == 1
    assert len(probes[0]) <= session_resume._REPLAY_TEXT_LIMIT * 2
    assert offloads == [
        (
            _capture,
            (probes[0], session_resume._REPLAY_TEXT_LIMIT),
            {
                "reserve": session_resume._REPLAY_RESERVE,
                "redactor": session_resume._default_redactor,
            },
        )
    ]
    assert preview == "safe preview" + session_resume._REPLAY_TRUNCATED


@pytest.mark.asyncio
async def test_a_long_at_bearing_transcript_preview_keeps_its_content() -> None:
    """A replayed transcript with an ``@`` is not emptied to the truncation marker.

    The splitter's credential predicate must be idempotent: the mention-defuser
    (``@`` -> ``@\u200b``) is not, so handing it to the splitter made
    ``canonical_shows_a_key`` permanently true on any ``@``-bearing body over the
    probe size and collapsed the preview to a whitespace-free blob or the bare
    truncation marker. The predicate is the real credential redactor; the probe
    is mention-defused before the split, once.
    """
    body = (
        "Here is some ordinary transcript text with an email me@example.com "
        "and an @property reference. " * 40
    )
    assert len(body) > session_resume._REPLAY_TEXT_LIMIT
    assert "@" in body
    preview = await session_resume._replay_preview(
        body,
        session_resume._REPLAY_TEXT_LIMIT,
        reserve=session_resume._REPLAY_RESERVE,
    )
    assert preview.strip() != session_resume._REPLAY_TRUNCATED.strip(), "the preview was emptied"
    assert "ordinary transcript text" in preview, "real content was stripped out"


@pytest.mark.asyncio
async def test_cold_resume_does_not_stamp_channel_or_retitle() -> None:
    """A cold resumed session must not get new-session bookkeeping.

    The picker lists *history*, so most picks are not live and `get_or_create`
    returns is_new=True. Treating that as a new session would (a) write
    `discord:<id>` into the dashboard session's legacy slack_channel_id — which
    survives `!unlink` and makes every later pick refuse with "already active on
    Slack" — and (b) overwrite the conversation's title with the Discord message.
    """
    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior work"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert "dashboard:chat-1" in sessions.inbound_keys

    # Cold ACP session: the normal case for a picked history session.
    sessions.is_new_result = True
    await dispatcher.handle_message(_message("continue here"))

    assert sessions.last_key == "dashboard:chat-1"
    # No channel stamp onto the resumed dashboard session...
    assert getattr(sessions, "set_channel_calls", []) == []
    # ...and its title is untouched.
    assert getattr(log, "titles_set", []) == []
    assert dispatcher.ctx_builder.last_build_kwargs["runtime_source"] == "discord"


@pytest.mark.asyncio
async def test_own_session_still_gets_new_session_bookkeeping() -> None:
    """The guard must not disable bookkeeping for Discord's OWN conversation."""
    log = _log()
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("hello there"))

    assert [key for key, _ in getattr(sessions, "set_channel_calls", [])] == [sessions.last_key]
    assert [key for key, _ in getattr(log, "titles_set", [])] == [sessions.last_key]


@pytest.mark.asyncio
async def test_own_session_records_the_origin_conversation() -> None:
    """The auto-compact notice needs the REAL channel, not the DM's user-id bucket."""
    log = _log()
    dispatcher, _, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("hello there", channel_id="c1"))

    link = sessions.get_origin_link(sessions.last_key)
    assert link is not None
    assert (link.channel_type, link.channel_id) == ("discord", "c1")


@pytest.mark.asyncio
async def test_new_own_session_surfaces_in_dashboard_immediately(monkeypatch) -> None:
    """Discord must not wait for the 30-second lifetime reconcile pass."""
    from kiro_crew.dashboard import channel_slots

    log = _log()
    dispatcher, _, sessions = _dispatcher({"u1"}, log)
    sessions.is_new_result = True
    state = object()
    dispatcher._session_resume.dashboard_state = state
    calls: list[tuple[Any, int]] = []

    async def _reconcile(candidate: Any, window_minutes: int) -> int:
        calls.append((candidate, window_minutes))
        return 1

    monkeypatch.setattr(channel_slots, "reconcile_channel_slots", _reconcile)

    await dispatcher.handle_message(_message("hello there"))

    assert calls == [(state, 30)]


@pytest.mark.asyncio
async def test_resumed_session_does_not_surface_duplicate_dashboard_slot(monkeypatch) -> None:
    """A Discord-driven dashboard resume already owns a slot."""
    from kiro_crew.dashboard import channel_slots

    log = _log()
    log.messages["dashboard:chat-1"] = [{"role": "assistant", "content": "prior"}]
    dispatcher, client, sessions = _dispatcher({"u1"}, log)
    calls: list[tuple[Any, int]] = []

    async def _reconcile(candidate: Any, window_minutes: int) -> int:
        calls.append((candidate, window_minutes))
        return 1

    monkeypatch.setattr(channel_slots, "reconcile_channel_slots", _reconcile)
    await dispatcher.handle_message(_message("!sessions"))
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    sessions.is_new_result = True

    await dispatcher.handle_message(_message("continue here"))

    assert calls == []
