"""Backfill fidelity: what a freshly linked thread is seeded with.

Covers the four defects the old implementation had -- a window sliced before the
role filter, a window measured in raw rows rather than turns, a 2,000-char drop
instead of a split, and no mrkdwn conversion -- plus the redaction ordering and
the off-window first turn.

The drain is a background task in production, so these tests await
``drain_slack_backfill`` directly rather than sleeping and hoping. One test drives
the real HTTP endpoint and drains ``state._background_tasks`` to prove the
handler wiring.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_chat_slack import _make_slack_app

from kiro_crew.dashboard.chat_backfill import select_backfill_messages
from kiro_crew.dashboard.chat_slack import drain_slack_backfill
from kiro_crew.slack.format import SLACK_MAX_TEXT, SLACK_MSG_LIMIT


def _state(tmp_path):
    state = _make_state(tmp_path)
    state.slack_client = MagicMock()
    state.slack_client.open_dm = AsyncMock(return_value="D123")
    state.slack_client.post_message = AsyncMock(return_value="newts")
    state.owner_id = "U123"
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.set_slack_link = MagicMock()
    state.push_slots_update = MagicMock()
    return state


def _seed(slot, rows):
    """Seed via the production append/drain path, not raw list surgery."""
    for role, content in rows:
        slot.append(role, content)
    slot.drain()


def _posted(state):
    """Every text posted to Slack, in order."""
    return [call.args[1] for call in state.slack_client.post_message.await_args_list]


async def _drain(state, slot):
    await drain_slack_backfill(state, slot, "C1", "1700.1")
    return _posted(state)


class TestBackfillSelection:
    """Turn selection — the unit is a turn, and the filter precedes the slice."""

    @pytest.mark.asyncio
    async def test_filter_runs_before_slice(self, tmp_path):
        """A tail of tool rows must not consume the window.

        This is the regression that motivated the change: the old code took
        ``slot.messages[-5:]`` and only then dropped non-conversational roles, so
        three trailing tool rows left three empty slots and five would have
        seeded nothing at all.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(
            slot,
            [
                ("user", "why is the build red"),
                ("assistant", "a lint rule changed"),
                ("tool", "grep ..."),
                ("tool", "cat ..."),
                ("tool", "pytest ..."),
            ],
        )
        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert "why is the build red" in body
        assert "a lint rule changed" in body
        assert not any("grep" in text or "pytest" in text for text in posted)

    @pytest.mark.asyncio
    async def test_five_consecutive_tool_rows_still_seed_the_turn(self, tmp_path):
        """The pathological case: the whole raw tail is non-conversational."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(
            slot,
            [("user", "run it"), ("assistant", "done")] + [("tool", f"step {i}") for i in range(5)],
        )
        posted = await _drain(state, slot)
        assert any("run it" in text for text in posted)
        assert any("done" in text for text in posted)

    @pytest.mark.asyncio
    async def test_first_turn_plus_last_five_turns_with_gap_marker(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        rows = []
        for i in range(1, 11):
            rows.append(("user", f"question {i}"))
            rows.append(("assistant", f"answer {i}"))
        _seed(slot, rows)

        posted = await _drain(state, slot)
        body = "\n".join(posted)

        assert "question 1" in body and "answer 1" in body
        for i in range(6, 11):
            assert f"question {i}" in body, f"turn {i} missing"
            assert f"answer {i}" in body
        for i in range(2, 6):
            assert f"question {i}" not in body, f"turn {i} should be skipped"

        markers = [text for text in posted if "earlier turn" in text]
        assert len(markers) == 1, f"expected exactly one gap marker, got {markers}"
        assert "4 earlier turns" in markers[0]

    @pytest.mark.asyncio
    async def test_gap_marker_sits_between_first_turn_and_recent_window(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        rows = []
        for i in range(1, 11):
            rows.append(("user", f"question {i}"))
            rows.append(("assistant", f"answer {i}"))
        _seed(slot, rows)

        posted = await _drain(state, slot)
        marker_at = next(i for i, text in enumerate(posted) if "earlier turn" in text)
        first_at = next(i for i, text in enumerate(posted) if "question 1" in text)
        recent_at = next(i for i, text in enumerate(posted) if "question 6" in text)
        assert first_at < marker_at < recent_at

    @pytest.mark.asyncio
    async def test_overlap_posts_contiguously_without_duplicating(self, tmp_path):
        """Three turns fit inside the recent window: no marker, no repeat."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(
            slot,
            [
                ("user", "alpha"),
                ("assistant", "alpha reply"),
                ("user", "beta"),
                ("assistant", "beta reply"),
                ("user", "gamma"),
                ("assistant", "gamma reply"),
            ],
        )
        posted = await _drain(state, slot)
        assert not any("earlier turn" in text for text in posted)
        assert sum(1 for text in posted if "alpha" in text and "reply" not in text) == 1

    @pytest.mark.asyncio
    async def test_six_turns_is_contiguous_with_no_marker(self, tmp_path):
        """Boundary: turn 1 is outside the 5-turn window but nothing is skipped."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        rows = []
        for i in range(1, 7):
            rows.append(("user", f"q{i}"))
            rows.append(("assistant", f"a{i}"))
        _seed(slot, rows)

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert not any("earlier turn" in text for text in posted)
        for i in range(1, 7):
            assert f"q{i}" in body
        assert sum(1 for text in posted if "q1" in text) == 1

    @pytest.mark.asyncio
    async def test_compaction_rows_are_neither_posted_nor_counted(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "real question")
        slot.append("assistant", "real answer")
        slot.append("assistant", "context compacted", meta={"kind": "compaction"})
        slot.drain()

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert "real question" in body and "real answer" in body
        assert "context compacted" not in body

    @pytest.mark.asyncio
    async def test_non_dict_meta_does_not_crash_selection(self, tmp_path):
        """``append``'s ``meta: dict | None`` is not enforced at runtime."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "hi"), ("assistant", "hello")])
        slot.messages.append({"role": "assistant", "content": "odd", "meta": "not-a-dict"})

        selection = select_backfill_messages(state, slot)
        assert [row["content"] for row in selection.messages] == ["hi", "hello", "odd"]

    @pytest.mark.asyncio
    async def test_empty_slot_posts_nothing(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        posted = await _drain(state, slot)
        assert posted == []


class TestBackfillFormatting:
    """Redact, convert, split — in that order, with nothing dropped."""

    @pytest.mark.asyncio
    async def test_long_message_is_split_not_truncated(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        long_answer = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
        assert len(long_answer) > 9000
        _seed(slot, [("user", "explain"), ("assistant", long_answer)])

        posted = await _drain(state, slot)
        answer_parts = [text for text in posted if "line 0 " in text or "line 119 " in text]
        assert len(posted) >= 3, f"expected the answer to span parts, got {len(posted)}"
        assert all(len(text) <= SLACK_MSG_LIMIT for text in posted)
        # Every line survives somewhere — the old 2,000-char cap dropped the rest.
        body = "\n".join(posted)
        for i in (0, 60, 119):
            assert f"line {i} " in body, f"line {i} lost"
        assert answer_parts

    @pytest.mark.asyncio
    async def test_markdown_is_converted_to_slack_mrkdwn(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "doc it"), ("assistant", "## Heading\n\n**bold** text")])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert "## Heading" not in body, "literal markdown heading leaked"
        assert "*Heading*" in body
        assert "**bold**" not in body
        assert "*bold*" in body

    @pytest.mark.asyncio
    async def test_credentials_are_redacted(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        _seed(slot, [("user", "here are creds"), ("assistant", f"key is {secret}")])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert secret not in body, "credential reached Slack unredacted"

    @pytest.mark.asyncio
    async def test_table_straddling_a_split_keeps_every_row(self, tmp_path):
        """A split must not let a data row become the table's header.

        ``_convert_tables`` labels a table from the first ``|`` row it sees, so a
        block starting part-way through one adopts a DATA row as its header. That
        row's values then only ever appear as labels — and disappear completely if
        no data rows follow — while later rows get the wrong labels. Tables are
        therefore left raw whenever the pre-split produced more than one block.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        rows = [f"| r{i:04d} | v{i:04d} |" for i in range(1400)]
        table = "| name | value |\n| --- | --- |\n" + "\n".join(rows)
        assert len(table) > SLACK_MAX_TEXT // 2, "table must be big enough to split"
        _seed(slot, [("user", "show the table"), ("assistant", table)])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert all(len(text) <= SLACK_MSG_LIMIT for text in posted)
        # Every emitted label must come from the REAL header row. A data row
        # adopted as the header shows up as a label like "*r1080:*" — checking the
        # label SET rather than one hardcoded token means the assertion holds
        # wherever the split happens to land.
        labels = set(re.findall(r"\*([^*:\n]+):\*", body))
        assert labels <= {"name", "value"}, f"data row adopted as header: {labels}"
        for i in (0, 700, 1399):
            assert f"r{i:04d}" in body, f"row {i} lost"
            assert f"v{i:04d}" in body, f"row {i} value lost"

    @pytest.mark.asyncio
    async def test_small_table_still_converts_for_readability(self, tmp_path):
        """The raw-pipe fallback must not apply to messages that never split."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        table = "| name | value |\n| --- | --- |\n| alpha | 1 |\n| beta | 2 |"
        _seed(slot, [("user", "show it"), ("assistant", table)])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert "*name:*" in body, "small table lost its vertical-list conversion"
        assert "alpha" in body and "beta" in body

    @pytest.mark.asyncio
    async def test_message_past_the_mrkdwn_limit_keeps_its_tail(self, tmp_path):
        """Splitting must happen around ``to_slack_mrkdwn``, not after it.

        ``to_slack_mrkdwn`` self-truncates at ``SLACK_MAX_TEXT``, so converting
        the whole message and splitting the result silently drops everything past
        39,000 characters — the same tail loss as the 2,000-char cap this
        replaces, just further out. The text is pre-split below the limit so no
        block ever reaches that internal truncation.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        # Distinct markers spread across ~60k chars so a lost tail is provable.
        answer = "\n".join(f"marker{i:05d} " + "z" * 40 for i in range(1400))
        assert len(answer) > SLACK_MAX_TEXT * 1.4
        _seed(slot, [("user", "dump it"), ("assistant", answer)])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert all(len(text) <= SLACK_MSG_LIMIT for text in posted)
        for i in (0, 700, 1399):
            assert f"marker{i:05d}" in body, f"marker {i} lost — the tail was truncated"
        assert "truncated (" not in body, "to_slack_mrkdwn's own truncation notice leaked"

    @pytest.mark.asyncio
    async def test_ansi_credential_straddling_a_split_boundary_is_redacted(self, tmp_path):
        """The hard case: obfuscated AND cut across two posts.

        If ANSI is only stripped per block (inside ``to_slack_mrkdwn``), a
        credential broken by an escape and cut by the pre-split leaves each block
        holding an unmatchable fragment. Both blocks redact clean individually,
        and the two adjacent posts hand the reader the whole key. Normalising the
        escapes before the FIRST redaction — while the text is still one piece —
        is what closes it.

        The preconditions below are asserted rather than assumed: an earlier
        version of this test put the cut inside the filler instead of inside the
        credential, so the second redaction pass caught it and the test passed
        even with the fix removed.
        """
        from kiro_crew.slack.format import CONTINUATION

        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        split_secret = secret[:6] + "\x1b[0m" + secret[6:]

        # split_message reserves room for CONTINUATION and, with no newline to
        # snap to, cuts exactly there.
        boundary = SLACK_MAX_TEXT // 2
        cut = boundary - len(CONTINUATION)
        filler = "q" * (cut - 12)
        content = filler + split_secret + ("z" * 200)

        assert len(content) > boundary, "no split would happen at all"
        assert len(filler) < cut < len(filler) + len(split_secret), (
            "the credential must straddle the cut for this test to mean anything"
        )
        _seed(slot, [("user", "paste"), ("assistant", content)])

        posted = await _drain(state, slot)
        body = "".join(posted)
        assert secret not in body, "credential reassembled across the split boundary"
        assert secret[:8] not in body, "a credential fragment reached Slack"

    @pytest.mark.asyncio
    async def test_conversion_cannot_reassemble_a_credential(self, tmp_path):
        """Redaction must run on BOTH sides of the mrkdwn transform.

        ``to_slack_mrkdwn`` strips ANSI escapes before converting. A credential
        broken up by an escape sequence therefore does not match the regex on the
        way in, and would be reassembled *intact* by the strip — arriving at
        Slack complete. Redacting the converted text closes that path.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        # An ANSI reset dropped into the middle of the key: invisible to the
        # credential regex, removed entirely by _strip_ansi.
        split_secret = secret[:4] + "\x1b[0m" + secret[4:]
        _seed(slot, [("user", "paste"), ("assistant", f"key is {split_secret}")])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert secret not in body, "conversion reassembled the credential"

    @pytest.mark.asyncio
    async def test_credential_straddling_the_mrkdwn_boundary_is_redacted(self, tmp_path):
        """Ordering guard: redaction must precede ``to_slack_mrkdwn``.

        ``to_slack_mrkdwn`` self-truncates at ``SLACK_MAX_TEXT`` (39,000) before
        it converts anything. Converting first would therefore hand the redactor
        a credential that has already been cut in half, leaving a prefix no
        regex matches -- a partial secret on the wire.

        The padding is deliberately newline-free so ``rfind("\\n")`` returns -1
        and the cut lands exactly on 39,000, and the secret starts 10 chars
        before it, so convert-first leaves precisely its first 10 characters
        behind. A secret placed entirely past the boundary would simply be
        deleted, which is why the obvious version of this test proves nothing.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        filler = "x" * (SLACK_MAX_TEXT - 10)
        content = filler + secret
        assert len(content) > SLACK_MAX_TEXT
        assert "\n" not in filler
        _seed(slot, [("user", "dump"), ("assistant", content)])

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert secret not in body
        assert secret[:8] not in body, "a truncated credential prefix reached Slack"

    @pytest.mark.asyncio
    async def test_roles_are_labelled_distinctly(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "ping"), ("assistant", "pong")])

        posted = await _drain(state, slot)
        user_post = next(text for text in posted if "ping" in text)
        agent_post = next(text for text in posted if "pong" in text)
        assert user_post[0] != agent_post[0], "user and agent posts share an icon"


class TestBackfillOffWindowFirstTurn:
    """The opening turn can live only on disk."""

    @pytest.mark.asyncio
    async def test_first_turn_read_from_transcript(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        key = "dashboard:s1"
        # Older turns exist only in the transcript, as they would after a
        # restart trimmed the in-memory window.
        state.conversation_log.append(key, "user", "the original question")
        state.conversation_log.append(key, "assistant", "the original answer")
        for i in range(2, 9):
            state.conversation_log.append(key, "user", f"disk q{i}")
            state.conversation_log.append(key, "assistant", f"disk a{i}")
        _seed(slot, [("user", "recent q"), ("assistant", "recent a")])
        slot._disk_older_count = 16

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert "the original question" in body, "first turn not recovered from disk"
        assert "the original answer" in body
        assert "recent q" in body and "recent a" in body
        assert any("earlier turn" in text for text in posted)

    @pytest.mark.asyncio
    async def test_gap_count_is_exact_across_the_flush_boundary(self, tmp_path):
        """Prefix and live window are grouped as ONE sequence.

        The transcript can lag the live window, so counting turns separately on
        each side undercounts the gap and can split one turn in two. Slicing the
        transcript to ``_disk_older_count`` — exactly the rows the window does
        not hold — and grouping the concatenation fixes both.
        """
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        key = "dashboard:s1"
        # 8 turns on disk; the 8th turn's ANSWER is still only in memory.
        rows_on_disk = 0
        for i in range(1, 9):
            state.conversation_log.append(key, "user", f"q{i}")
            rows_on_disk += 1
            if i < 8:
                state.conversation_log.append(key, "assistant", f"a{i}")
                rows_on_disk += 1
        _seed(slot, [("assistant", "a8"), ("user", "q9"), ("assistant", "a9")])
        slot._disk_older_count = rows_on_disk

        selection = select_backfill_messages(state, slot)
        # 9 turns total, 5 recent (turns 5-9), turn 1 first, so 3 skipped (2-4).
        assert [r["content"] for r in selection.first_turn] == ["q1", "a1"]
        assert selection.skipped_turns == 3
        # a8 came from memory but belongs to q8's turn, not its own.
        turn_openers = [turn[0]["content"] for turn in selection.recent]
        assert turn_openers == ["q5", "q6", "q7", "q8", "q9"]
        assert [r["content"] for r in selection.recent[3]] == ["q8", "a8"]

    @pytest.mark.asyncio
    async def test_transcript_not_consulted_when_window_is_complete(self, tmp_path):
        """``_disk_older_count == 0`` means memory already holds turn 1."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.conversation_log.read_messages_chained = MagicMock(
            side_effect=AssertionError("transcript must not be read")
        )
        _seed(slot, [("user", "only q"), ("assistant", "only a")])
        assert slot._disk_older_count == 0

        posted = await _drain(state, slot)
        assert any("only q" in text for text in posted)

    @pytest.mark.asyncio
    async def test_transcript_read_failure_degrades_to_memory(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.conversation_log.read_messages_chained = MagicMock(
            side_effect=OSError("disk gone")
        )
        rows = []
        for i in range(1, 8):
            rows.append(("user", f"q{i}"))
            rows.append(("assistant", f"a{i}"))
        _seed(slot, rows)
        slot._disk_older_count = 4

        posted = await _drain(state, slot)
        body = "\n".join(posted)
        assert "q7" in body, "recent window lost when the transcript failed"
        assert "q1" in body, "fell back to memory for the first turn"

    @pytest.mark.asyncio
    async def test_shared_transcript_cache_is_not_mutated(self, tmp_path):
        """``read_messages_chained`` may hand back the shared cached list."""
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        key = "dashboard:s1"
        for i in range(1, 9):
            state.conversation_log.append(key, "user", f"q{i}")
            state.conversation_log.append(key, "assistant", f"a{i}")
        cached = state.conversation_log.read_messages_chained(key)
        before = list(cached)
        _seed(slot, [("user", "recent q"), ("assistant", "recent a")])
        slot._disk_older_count = 16

        await _drain(state, slot)
        assert state.conversation_log.read_messages_chained(key) == before


class TestBackfillHandlerWiring:
    """The endpoint must background the drain and honour the existing-thread guard."""

    @pytest.mark.asyncio
    async def test_link_backgrounds_the_drain_and_seeds_the_thread(self, tmp_path, monkeypatch):
        """The response must not wait on the backfill.

        Slack accepts about one message per second per channel, so a long
        history held the request open long enough for the browser fetch to time
        out. The drain is blocked here on an event so the test can prove the
        handler returned with the seeding still in flight, rather than racing a
        drain that happens to finish instantly against AsyncMock.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "seed me"), ("assistant", "seeded")])

        release = asyncio.Event()
        sent: list[str] = []

        async def _post(channel, text, thread_ts=None):
            # Record AFTER the gate so `sent` holds only COMPLETED posts: the
            # drain is allowed to start during the handler's remaining awaits,
            # but it must not have finished seeding when the response goes out.
            if thread_ts is not None:  # the anchor has no thread_ts; backfill does
                await release.wait()
            sent.append(text)
            return "newts"

        state.slack_client.post_message = AsyncMock(side_effect=_post)

        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200

        assert sent, "anchor was never posted"
        assert "Session linked from dashboard" in sent[0], "anchor must be posted first"
        # sent[0] is the anchor, whose title legitimately quotes the first user
        # prompt — so only the posts AFTER it can evidence a completed backfill.
        assert sent[1:] == [], (
            "the request waited for the backfill instead of backgrounding it"
        )

        pending = [t for t in state._background_tasks if not t.done()]
        assert pending, "link did not register a background drain"
        release.set()
        await asyncio.gather(*pending)

        body = "\n".join(sent[1:])
        assert "seed me" in body and "seeded" in body

    @pytest.mark.asyncio
    async def test_existing_thread_link_seeds_nothing(self, tmp_path, monkeypatch):
        """Challenge-and-redirect: the thread already holds this history."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "hello"), ("assistant", "hi there")])

        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/slack-link",
                json={"channel": "C999", "thread_ts": "1700.42"},
            )
            assert resp.status == 200
        for task in [t for t in state._background_tasks if not t.done()]:
            await task
        assert state.slack_client.post_message.await_count == 0

    @pytest.mark.asyncio
    async def test_post_failure_aborts_without_raising(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "one"), ("assistant", "two")])
        state.slack_client.post_message = AsyncMock(side_effect=RuntimeError("slack down"))

        await drain_slack_backfill(state, slot, "C1", "1700.1")

    @pytest.mark.asyncio
    async def test_no_slack_client_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed(slot, [("user", "one"), ("assistant", "two")])
        state.slack_client = None

        await drain_slack_backfill(state, slot, "C1", "1700.1")
