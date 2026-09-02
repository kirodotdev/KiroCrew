"""Tests for fresh-slot first-turn duplicate user-message injection.

Race condition: dashboard chat handler appends the user message to slot.messages
and the periodic flush loop (5 s) writes it to JSONL. Meanwhile _run_chat awaits
get_or_create which spawns kiro-cli (~15 s cold). When kiro is finally up, the
context-builder reads the JSONL — and finds the user's CURRENT message there,
which it then injects as "history" alongside the same message under
[CURRENT USER REQUEST].

The fix: ConversationLog.recent / recent_chained / recent_with_provenance now
accept ``exclude_last_n`` to drop trailing raw entries before role filtering.
The dashboard caller (chat_runner._run_chat) passes exclude_last_n=1 because
exactly one message is appended per turn before _run_chat invokes context
build.
"""
from __future__ import annotations

from kiro_crew.context import build_session_replay
from kiro_crew.history import ConversationLog


class TestRecentExcludeLastN:
    def test_recent_default_returns_everything(self, tmp_path):
        """exclude_last_n defaults to 0 — backward-compatible."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "user", "u2")

        result = log.recent("k", roles={"user", "assistant"})

        assert [m["content"] for m in result] == ["u1", "a1", "u2"]

    def test_recent_exclude_last_n_drops_trailing_user(self, tmp_path):
        """exclude_last_n=1 drops the just-flushed current-turn user message."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "user", "u_current")  # the racing flush

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert [m["content"] for m in result] == ["u1", "a1"]

    def test_recent_exclude_applies_before_role_filter(self, tmp_path):
        """exclude_last_n drops raw entries BEFORE role filter, so a trailing
        inject/subagent entry doesn't cause a legitimate user/assistant entry
        to be dropped instead.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "subagent", "[Subagent completion event] …")

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert [m["content"] for m in result] == ["u1", "a1"]

    def test_recent_exclude_zero_disk_no_crash(self, tmp_path):
        """exclude_last_n on an empty log returns []."""
        log = ConversationLog(base_dir=tmp_path)

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert result == []

    def test_recent_exclude_larger_than_disk(self, tmp_path):
        """If exclude_last_n exceeds total entries, slice returns []."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")

        result = log.recent("k", roles={"user", "assistant"}, exclude_last_n=5)

        assert result == []

    def test_recent_with_provenance_exclude_last_n(self, tmp_path):
        """recent_with_provenance also honors exclude_last_n."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1", source_thread="dashboard")
        log.append("k", "assistant", "a1", source_thread="dashboard")
        log.append("k", "user", "u_current", source_thread="dashboard")

        result = log.recent_with_provenance("k", exclude_last_n=1)

        assert [r["snippet"] for r in result] == ["u1", "a1"]

    def test_recent_chained_exclude_last_n(self, tmp_path):
        """recent_chained falls back to single-file read for sessions without
        tab_id, so exclude_last_n behaves identically to recent() in that case.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "u1")
        log.append("k", "assistant", "a1")
        log.append("k", "user", "u_current")

        result = log.recent_chained("k", roles={"user", "assistant"}, exclude_last_n=1)

        assert [m["content"] for m in result] == ["u1", "a1"]


class TestBuildSessionReplayMesh1726:
    def test_build_session_replay_drops_current_turn(self, tmp_path):
        """End-to-end: build_session_replay with exclude_last_n=1 returns None
        when the only on-disk message is the just-flushed current-turn user msg.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "what time is it?")

        replay = build_session_replay(log, "k", exclude_last_n=1)

        assert replay is None

    def test_build_session_replay_keeps_real_history(self, tmp_path):
        """Sanity: with prior turns on disk, exclude_last_n=1 keeps them."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "earlier question")
        log.append("k", "assistant", "earlier answer")
        log.append("k", "user", "current question")  # the race-flushed msg

        replay = build_session_replay(log, "k", exclude_last_n=1)

        assert replay is not None
        assert "earlier question" in replay
        assert "earlier answer" in replay
        assert "current question" not in replay

    def test_build_session_replay_default_behavior_unchanged(self, tmp_path):
        """Backward compat: exclude_last_n=0 (default) preserves existing behavior."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "msg1")
        log.append("k", "assistant", "resp1")

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert "msg1" in replay
        assert "resp1" in replay

    def test_build_session_replay_includes_inject_role(self, tmp_path):
        """inject-role messages (cron results, /note breadcrumbs) are included in
        session replay so the agent recalls them across session boundaries."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "start the cron")
        log.append("k", "assistant", "cron started")
        log.append("k", "inject", "[Cron result] board reconciled: 3 sessions moved to Done")
        log.append("k", "user", "what happened while I was away?")

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert "board reconciled" in replay
        assert "start the cron" in replay

    def test_build_session_replay_excludes_system_role(self, tmp_path):
        """system-role messages are internal (thinking, done markers) and stay
        excluded from replay -- inject is the only visible breadcrumb role."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log.append("k", "assistant", "hi")
        log.append("k", "system", "internal system message")

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert "hello" in replay
        assert "internal system message" not in replay

    def test_a_flood_of_inject_rows_cannot_evict_conversation(self, tmp_path):
        """A long run of inject rows must not push every user/assistant turn out
        of the replay. The per-row character ceiling cannot prevent this: the row
        count is bounded before any budgeting runs, so without a separate quota a
        tail of inject rows is all the replay would contain.
        """
        from kiro_crew.context import _REPLAY_CONVERSATION_MAX_ROWS

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "first question")
        log.append("k", "assistant", "first answer")
        for i in range(_REPLAY_CONVERSATION_MAX_ROWS + 100):
            log.append("k", "inject", f"[Cron result] tick {i}")

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert "first question" in replay
        assert "first answer" in replay

    def test_inject_rows_are_bounded_by_their_own_quota(self, tmp_path):
        """The inject quota is enforced, so a chatty producer contributes at most
        its share of rows however many it wrote.
        """
        from kiro_crew.context import _REPLAY_INJECT_MAX_ROWS

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "first question")
        for i in range(_REPLAY_INJECT_MAX_ROWS + 50):
            log.append("k", "inject", f"[Cron result] tick {i}")

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert replay.count("[Cron result] tick") <= _REPLAY_INJECT_MAX_ROWS

    def test_capped_inject_rows_cannot_spend_the_whole_char_budget(self, tmp_path):
        """Conversation keeps its reserved share of the replay budget.

        The row quota alone does not achieve this: it decides which rows are
        SELECTED, while the character budget is spent afterwards newest-first, so
        without a reservation a run of maximum-sized inject rows exhausts it and
        the loop breaks before reaching any user or assistant row.
        """
        from kiro_crew.context import _REPLAY_INJECT_CAP_CHARS, _REPLAY_INJECT_MAX_ROWS

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "first question")
        log.append("k", "assistant", "first answer")
        for i in range(_REPLAY_INJECT_MAX_ROWS + 5):
            log.append("k", "inject", f"{i:04d}" + "x" * _REPLAY_INJECT_CAP_CHARS)

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert "first question" in replay
        assert "first answer" in replay

    def test_inject_rows_stay_within_their_reserved_share(self, tmp_path):
        """The inject share is bounded, so breadcrumbs cannot crowd the budget."""
        from kiro_crew.context import (
            _REPLAY_BUDGET_CHARS,
            _REPLAY_INJECT_BUDGET_DIVISOR,
            _REPLAY_INJECT_CAP_CHARS,
            _REPLAY_INJECT_MAX_ROWS,
        )

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "first question")
        for i in range(_REPLAY_INJECT_MAX_ROWS + 5):
            log.append("k", "inject", f"{i:04d}" + "x" * _REPLAY_INJECT_CAP_CHARS)

        replay = build_session_replay(log, "k")

        assert replay is not None
        inject_chars = sum(
            len(block) for block in replay.split("\n\n") if block.startswith("Inject: ")
        )
        share = _REPLAY_BUDGET_CHARS // _REPLAY_INJECT_BUDGET_DIVISOR
        assert inject_chars <= share

    def test_recall_roles_constant_includes_inject(self):
        """RECALL_ROLES is the single source of truth for which roles survive
        session replay and compression. Verify inject is present so /note
        breadcrumbs and cron results are recalled."""
        from kiro_crew.context import RECALL_ROLES

        assert "inject" in RECALL_ROLES
        assert "user" in RECALL_ROLES
        assert "assistant" in RECALL_ROLES
        # system must stay excluded (thinking/done markers, not visible content)
        assert "system" not in RECALL_ROLES

    def test_build_session_replay_uses_recall_roles_constant(self, tmp_path):
        """build_session_replay filters via RECALL_ROLES, not a hardcoded literal.
        Adding a role to the constant is picked up by replay without touching the
        function."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "hello")
        log.append("k", "inject", "[Note] session moved to Done")
        log.append("k", "assistant", "noted")
        log.append("k", "system", "internal thinking")

        replay = build_session_replay(log, "k")
        assert replay is not None
        # inject IS in RECALL_ROLES -> appears in replay
        assert "session moved to Done" in replay
        # system is NOT in RECALL_ROLES -> excluded from replay
        assert "internal thinking" not in replay

    def test_replay_caps_oversized_inject_row(self, tmp_path):
        """A chatty producer's inject row is clipped to a breadcrumb in replay."""
        from kiro_crew.context import _REPLAY_INJECT_CAP_CHARS

        log = ConversationLog(base_dir=tmp_path)
        log.append("k", "user", "kick off the job")
        log.append("k", "inject", "[Cron result] " + "x" * 40_000)

        replay = build_session_replay(log, "k")

        assert replay is not None
        # replay normalizes the ellipsis to ASCII, so match the stable tail
        assert "[truncated]" in replay
        assert "[Cron result]" in replay, "the breadcrumb's head must survive the clip"
        assert replay.count("x") <= _REPLAY_INJECT_CAP_CHARS

    def test_replay_leaves_typical_inject_row_whole(self, tmp_path):
        """A breadcrumb under the cap is passed through untouched."""
        log = ConversationLog(base_dir=tmp_path)
        body = "[Note] " + "y" * 500
        log.append("k", "user", "hello")
        log.append("k", "inject", body)

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert body in replay
        assert "[truncated]" not in replay

    def test_replay_does_not_cap_conversation_rows(self, tmp_path):
        """The cap is inject-only: conversation rows are the signal replay carries."""
        log = ConversationLog(base_dir=tmp_path)
        long_user = "u" * 20_000
        log.append("k", "user", long_user)

        replay = build_session_replay(log, "k")

        assert replay is not None
        assert long_user in replay
        assert "[truncated]" not in replay

    def test_replay_inject_cap_preserves_conversation_history(self, tmp_path):
        """Regression: an oversized inject row must not evict real conversation.

        Fills the tail-heavy budget with conversation, then prepends a huge inject
        row at the newest end. Uncapped, that row spends budget the conversation
        needs and the oldest turns fall out of the replay.
        """
        from kiro_crew.context import _REPLAY_BUDGET_CHARS

        log = ConversationLog(base_dir=tmp_path)
        turn = "z" * 800
        n_turns = (_REPLAY_BUDGET_CHARS // len(turn)) + 20
        for i in range(n_turns):
            log.append("k", "user", f"{i:05d}-{turn}")
        log.append("k", "inject", "[Cron result] " + "q" * 40_000)

        replay = build_session_replay(log, "k")

        assert replay is not None
        kept = replay.count("-" + turn)
        # Without the cap the inject row alone would displace ~50 of these turns.
        assert kept >= n_turns - 25, f"only {kept} of {n_turns} conversation turns survived"


class TestBoundedRecallQuotas:
    """The two bounded ``recent()`` recall sites must not let inject volume
    evict conversation.

    ``recent()`` filters by role and then takes a plain tail slice, so a run of
    ``inject`` rows longer than the bound IS the whole read. ``build_session_replay``
    already defends this with per-role quotas; these two sites read through a
    single bounded query, which cannot give the same guarantee.
    """

    def test_a_note_flood_cannot_evict_conversation_from_the_cold_start_builder(self, tmp_path):
        """``build_session_context``'s fallback passes no ``max_messages``, so it
        takes ``recent()``'s 20-row default -- the real bound, not a chosen one.
        """
        import inspect

        from kiro_crew import context as ctx
        from kiro_crew.history import ConversationLog as _CL
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        # Pin the bound this test relies on rather than hard-coding 20 blindly.
        assert inspect.signature(_CL.recent).parameters["max_messages"].default == 20

        log = _CL(base_dir=tmp_path / "hist")
        key = "fallback-note-flood"
        log.append(key, "user", "REMEMBER_THE_ALPHA_REQUEST")
        log.append(key, "assistant", "ACKNOWLEDGED_BETA_REPLY")
        for i in range(25):  # > the 20-row default, notes alone
            log.append(key, "inject", f"[Note] tick {i}")

        builder = ctx.ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            conversation_log=log,
        )
        out = builder.build_session_context(session_key=key)

        assert "REMEMBER_THE_ALPHA_REQUEST" in out
        assert "ACKNOWLEDGED_BETA_REPLY" in out
        assert "[Note] tick" in out, "notes must still reach the model"

    def test_a_note_flood_cannot_evict_conversation_from_compression(self, tmp_path):
        """Same defect at the compression site, whose bound is 100 rather than 20."""
        import asyncio

        from kiro_crew import context as ctx
        from kiro_crew.history import ConversationLog as _CL

        log = _CL(base_dir=tmp_path)
        key = "compress-note-flood"
        log.append(key, "user", "REMEMBER_THE_ALPHA_REQUEST")
        log.append(key, "assistant", "ACKNOWLEDGED_BETA_REPLY")
        for i in range(ctx._COMPRESSION_MAX_MESSAGES + 25):
            log.append(key, "inject", f"[Note] tick {i}")

        # Short transcript stays under the compressed cap, so this returns the
        # transcript directly and never reaches the LLM branch (sessions unused).
        out = asyncio.run(ctx.compress_thread_history(log, key, "a query", None))

        assert out is not None
        assert "REMEMBER_THE_ALPHA_REQUEST" in out
        assert "ACKNOWLEDGED_BETA_REPLY" in out
        assert "[Note] tick" in out, "notes must still reach the model"

    def test_large_notes_cannot_spend_the_whole_fallback_budget(self, tmp_path):
        """Second, independent direction at the same site.

        The row quota admits conversation, but the fallback spends its character
        budget newest-first and notes ARE the newest rows -- so a handful of large
        ones exhaust it before any user or assistant turn is reached. Notes get a
        reserved share and a per-row ceiling, as in the replay path.
        """
        from kiro_crew import context as ctx
        from kiro_crew.history import ConversationLog as _CL
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        log = _CL(base_dir=tmp_path / "hist")
        key = "fallback-large-notes"
        log.append(key, "user", "REMEMBER_THE_ALPHA_REQUEST")
        log.append(key, "assistant", "ACKNOWLEDGED_BETA_REPLY")
        # Comfortably past the per-row ceiling, so each note is also truncated.
        for i in range(25):
            log.append(key, "inject", f"[Note] tick {i} " + ("Z" * 8_000))

        builder = ctx.ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            conversation_log=log,
        )
        out = builder.build_session_context(session_key=key)

        assert "REMEMBER_THE_ALPHA_REQUEST" in out
        assert "ACKNOWLEDGED_BETA_REPLY" in out
        assert "[Note] tick" in out, "notes must still reach the model"

    def test_the_compression_transcript_read_does_not_run_on_the_event_loop(self, tmp_path):
        """The quota walk needs the WHOLE file, so the read cannot be a cheap tail
        slice -- which makes where it runs matter. On the loop thread a cold parse
        stops every other gateway coroutine (no-blocking-call-on-event-loop).
        """
        import asyncio
        import threading
        from unittest.mock import patch

        from kiro_crew import context as ctx
        from kiro_crew.history import ConversationLog as _CL

        log = _CL(base_dir=tmp_path)
        key = "offloaded-read"
        log.append(key, "user", "REMEMBER_THE_ALPHA_REQUEST")
        log.append(key, "assistant", "ACKNOWLEDGED_BETA_REPLY")

        seen: list[int] = []
        real = _CL.read_messages

        def _recording(self, k):
            seen.append(threading.get_ident())
            return real(self, k)

        async def _run():
            with patch.object(_CL, "read_messages", _recording):
                out = await ctx.compress_thread_history(log, key, "a query", None)
            return threading.get_ident(), out

        loop_thread, out = asyncio.run(_run())

        assert out is not None
        assert "REMEMBER_THE_ALPHA_REQUEST" in out
        assert seen, "the transcript read never happened -- test proves nothing"
        assert all(t != loop_thread for t in seen), (
            "the whole-file transcript read ran on the event-loop thread"
        )
