"""``GET /api/sessions/search/messages``: the matching messages across chats."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_sessions_search_messages
from kiro_crew.dashboard.handlers.sessions_parts.message_search import (
    MAX_HITS_PER_SESSION,
    message_hits,
    message_query,
)
from kiro_crew.history import ConversationLog, transcript_lock_stems

pytestmark = pytest.mark.asyncio

URL = "/api/sessions/search/messages"


def _app(log: ConversationLog, *, request_app: str = "") -> web.Application:
    @web.middleware
    async def _caller(request, handler):
        if request_app:
            request["app"] = request_app
        return await handler(request)

    app = web.Application(middlewares=[_caller])
    app["state"] = SimpleNamespace(conversation_log=log)
    app.router.add_get(URL, api_sessions_search_messages)
    return app


async def _get(log: ConversationLog, query: str, **kw) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(log, **kw))) as client:
        resp = await client.get(f"{URL}?{query}")
        return resp.status, await resp.json()


async def test_each_matching_message_is_listed_with_its_index_and_role(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "where is the retry budget set")
    log.append("alpha", "assistant", "the retry budget lives in config.yaml")
    log.append("alpha", "user", "thanks")
    log.append("beta", "user", "unrelated words")

    status, body = await _get(log, "q=retry+budget")

    assert status == 200
    [row] = body["results"]
    assert row["key"] == "alpha"
    assert row["hit_count"] == 2
    assert [(h["index"], h["role"]) for h in row["hits"]] == [(0, "user"), (1, "assistant")]
    assert "retry budget" in row["hits"][1]["snippet"]


async def test_the_role_filter_keeps_only_that_speaker(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "what about kiwi")
    log.append("alpha", "assistant", "kiwi is a fruit")

    _, mine = await _get(log, "q=kiwi&role=user")
    _, bots = await _get(log, "q=kiwi&role=assistant")

    assert [h["role"] for h in mine["results"][0]["hits"]] == ["user"]
    assert [h["role"] for h in bots["results"][0]["hits"]] == ["assistant"]


async def test_a_session_with_no_hit_for_the_role_is_left_out(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "mango please")
    log.append("alpha", "assistant", "here you go")

    _, body = await _get(log, "q=mango&role=assistant")

    assert body["results"] == []


async def test_the_agent_and_date_filters_narrow_the_sessions(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "papaya notes")
    log.append("beta", "user", "papaya again")
    await asyncio.to_thread(log.update_metadata, "alpha", {"agent": "research"})
    await asyncio.to_thread(log.update_metadata, "beta", {"agent": "coder"})

    _, by_agent = await _get(log, "q=papaya&agent=research")
    _, future = await _get(log, "q=papaya&since=99999999999")

    assert [r["key"] for r in by_agent["results"]] == ["alpha"]
    assert future["results"] == []


@pytest.mark.parametrize(
    "query, code",
    [
        ("q=pear&role=robot", "invalid_role"),
        ("q=pear&since=yesterday", "invalid_since"),
        ("q=pear&since=nan", "invalid_since"),
    ],
)
async def test_an_unreadable_filter_is_refused(tmp_path, query, code):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "pear")

    status, body = await _get(log, query)

    assert status == 400
    assert body["code"] == code


async def test_a_short_query_returns_nothing(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "a")

    status, body = await _get(log, "q=a")

    assert (status, body) == (200, {"results": [], "agents": []})


async def test_hits_per_session_are_capped_but_all_are_counted(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    for n in range(MAX_HITS_PER_SESSION + 3):
        log.append("alpha", "user", f"lychee {n}")

    _, body = await _get(log, "q=lychee")

    row = body["results"][0]
    assert len(row["hits"]) == MAX_HITS_PER_SESSION
    assert row["hit_count"] == MAX_HITS_PER_SESSION + 3


async def test_excerpts_and_titles_are_redacted(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    content = "guava " + "filler " * 80 + "secret here"
    log.append("alpha", "user", content)
    await asyncio.to_thread(log.set_title, "alpha", "Guava title")
    seen: list[str] = []

    def _fake(text):
        seen.append(text)
        return "[REDACTED]"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions.redact", _fake)

    _, body = await _get(log, "q=guava")

    assert body["results"][0]["hits"][0]["snippet"] == "[REDACTED]"
    # The whole message is redacted before the excerpt is cut from it.
    assert content in seen
    assert body["results"][0]["title"] == "[REDACTED]"
    assert "Guava title" in seen


async def test_an_app_caller_sees_only_the_sessions_it_owns(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    log.append("mine", "user", "quince recipe")
    log.append("theirs", "user", "quince recipe")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions._app_owns_transcript",
        lambda _log, app, key: app == "notes" and key == "mine",
    )

    _, body = await _get(log, "q=quince", request_app="notes")

    assert [r["key"] for r in body["results"]] == ["mine"]


def test_message_hits_skips_non_text_and_excluded_rows():
    messages = [
        {"role": "user", "content": "plum one"},
        {"role": "tool", "content": "plum from a tool"},
        {"role": "assistant", "content": [{"type": "text", "text": "plum"}]},
        {"role": "assistant", "content": "plum but skip-me"},
        "not a row",
        {"role": "assistant", "content": "PLUM two", "ts": "t5", "meta": {"mid": "m5"}},
    ]

    hits, count = message_hits(
        messages, message_query("plum -skip-me"), frozenset({"user", "assistant"})
    )

    assert [h["index"] for h in hits] == [0, 5]
    assert count == 2
    assert hits[1] == {
        "index": 5,
        "role": "assistant",
        "snippet": "PLUM two",
        "ts": "t5",
        "mid": "m5",
    }


async def test_a_title_scope_narrows_the_chats_and_the_rest_finds_the_messages(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "nectarine plan")
    log.append("beta", "user", "nectarine plan")
    await asyncio.to_thread(log.set_title, "alpha", "Deploy notes")
    await asyncio.to_thread(log.set_title, "beta", "Other")

    _, scoped = await _get(log, "q=title:deploy+nectarine")
    _, title_only = await _get(log, "q=title:deploy")

    assert [r["key"] for r in scoped["results"]] == ["alpha"]
    assert title_only["results"] == []


async def test_a_title_scope_with_only_exclusions_lists_no_messages(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "nectarine plan")
    log.append("alpha", "assistant", "the plan is set")
    await asyncio.to_thread(log.set_title, "alpha", "Deploy notes")

    _, body = await _get(log, "q=title:deploy+-vinod")

    assert body["results"] == []
    assert message_query("title:deploy -vinod").needles == []
    assert message_query("-vinod").needles == []


async def test_keys_of_one_tab_chain_are_listed_once(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    log.append("old", "user", "apricot one")
    log.append("new", "user", "apricot two")
    chain = {"old": ["old", "new"], "new": ["old", "new"]}
    monkeypatch.setattr(log, "chained_keys", lambda key: chain[key])
    monkeypatch.setattr(
        log,
        "read_messages_chained_full",
        lambda key: [
            {"role": "user", "content": "apricot one"},
            {"role": "user", "content": "apricot two"},
        ],
    )

    _, body = await _get(log, "q=apricot")

    assert len(body["results"]) == 1
    assert body["results"][0]["hit_count"] == 2


async def test_an_app_skips_a_chain_holding_a_transcript_it_does_not_own(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    log.append("mine", "user", "fig jam")
    monkeypatch.setattr(log, "chained_keys", lambda key: ["theirs", "mine"])
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions._app_owns_transcript",
        lambda _log, app, key: key == "mine",
    )

    _, body = await _get(log, "q=fig", request_app="notes")

    assert body["results"] == []


def test_a_message_must_hold_every_word_and_a_short_word_alone_is_no_hit():
    messages = [
        {"role": "user", "content": "deploy to staging tonight"},
        {"role": "user", "content": "what is the token today"},
        {"role": "assistant", "content": "staging is down, deploy it to a new host"},
        {"role": "assistant", "content": "deploy later"},
    ]

    hits, count = message_hits(
        messages, message_query("deploy to staging"), frozenset({"user", "assistant"})
    )

    assert [h["index"] for h in hits] == [0, 2]
    assert count == 2


def test_since_drops_messages_older_than_the_bound():
    messages = [
        {"role": "user", "content": "kumquat old", "ts": "2026-01-01T00:00:00Z"},
        {"role": "user", "content": "kumquat new", "ts": "2026-11-15T00:00:00Z"},
        {"role": "user", "content": "kumquat undated"},
    ]
    since = 1_790_000_000.0  # between the two dated rows

    hits, _ = message_hits(messages, message_query("kumquat"), frozenset({"user"}), since)

    assert [h["index"] for h in hits] == [1, 2]


async def test_the_ranking_is_asked_for_its_whole_window_before_the_filters(tmp_path, monkeypatch):
    from kiro_crew.history_search import search_candidate_cap

    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "persimmon")
    asked: list[int] = []
    real = log.search_sessions

    def spy(q, limit, **kw):
        asked.append(limit)
        return real(q, limit, **kw)

    monkeypatch.setattr(log, "search_sessions", spy)

    await _get(log, "q=persimmon&role=assistant&agent=research")

    assert asked == [search_candidate_cap("persimmon")]


async def test_the_bots_of_every_ranked_chat_are_listed_beside_the_rows(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "durian notes")
    log.append("beta", "user", "durian again")
    log.append("gamma", "user", "nothing here")
    await asyncio.to_thread(log.update_metadata, "alpha", {"agent": "research"})
    await asyncio.to_thread(log.update_metadata, "beta", {"agent": "coder"})
    await asyncio.to_thread(log.update_metadata, "gamma", {"agent": "writer"})

    _, body = await _get(log, "q=durian&agent=research")

    assert [r["key"] for r in body["results"]] == ["alpha"]
    assert body["agents"] == ["coder", "research"]


async def test_an_app_judges_ownership_and_reads_under_the_chain_locks(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    log.append("mine", "user", "loquat jam")
    held: list[list[str]] = []
    in_lock = [False]
    verdicts: list[bool] = []
    real_locked = log.locked_stems

    @contextlib.contextmanager
    def spy(stems):
        stems = list(stems)
        with real_locked(stems):
            held.append(stems)
            in_lock[0] = True
            try:
                yield
            finally:
                in_lock[0] = False

    def owns(_log, app, key):
        verdicts.append(in_lock[0])
        return key == "mine"

    monkeypatch.setattr(log, "locked_stems", spy)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions._app_owns_transcript", owns)

    _, body = await _get(log, "q=loquat", request_app="notes")

    assert [r["key"] for r in body["results"]] == ["mine"]
    assert held[0] == list(transcript_lock_stems("mine"))
    assert verdicts[-1] is True


async def test_an_app_skips_a_chain_that_changed_before_its_locks_were_held(tmp_path, monkeypatch):
    log = ConversationLog(base_dir=tmp_path)
    log.append("mine", "user", "medlar jam")
    chains = iter([["mine"], ["mine", "theirs"]])
    monkeypatch.setattr(log, "chained_keys", lambda key: next(chains))
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.sessions._app_owns_transcript",
        lambda _log, app, key: key == "mine",
    )

    _, body = await _get(log, "q=medlar", request_app="notes")

    assert body["results"] == []


async def test_a_rotated_chat_ranks_on_its_live_file_as_the_sidebar_search_does(
    tmp_path, monkeypatch
):
    # The route takes the shared ranking, which reads each chat's live file: a
    # word only the rotated archive holds lists nothing here or in the sidebar
    # search, while a word in the live file is found with its message.
    monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
    monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
    log = ConversationLog(base_dir=tmp_path)
    log.append("alpha", "user", "tamarind is the word only the first message holds")
    for i in range(20):
        log.append("alpha", "user", f"filler message number {i:02d} with enough text to rotate")
    log.append("alpha", "user", "jackfruit sits in the newest message")
    assert list((tmp_path / "archive").glob("alpha__*.jsonl")), "fixture must rotate"
    assert all("tamarind" not in m.get("content", "") for m in log.read_messages("alpha"))

    _, archived = await _get(log, "q=tamarind")
    _, live = await _get(log, "q=jackfruit")

    assert archived["results"] == []
    assert log.search_sessions("tamarind", 10) == []
    assert [r["key"] for r in live["results"]] == ["alpha"]
    [hit] = live["results"][0]["hits"]
    corpus = log.read_messages_chained_full("alpha")
    assert corpus[hit["index"]]["content"] == "jackfruit sits in the newest message"


async def test_a_real_two_transcript_tab_is_listed_once(tmp_path):
    # Ranked rows carry file stems (`dashboard_chat-…`) while the tab index spells
    # the same transcripts `dashboard:chat-…`; the chain must still list once.
    log = ConversationLog(base_dir=tmp_path)
    tab = "tabchain0001"
    log.append("dashboard:chat-1-100", "user", "guanabana first half", tab_id=tab)
    log.append("dashboard:chat-2-200", "user", "guanabana second half", tab_id=tab)

    _, body = await _get(log, "q=guanabana")

    assert len(body["results"]) == 1
    assert body["results"][0]["hit_count"] == 2
