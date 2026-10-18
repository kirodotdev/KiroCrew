"""Contract tests for ``messaging/privacy_mode.py``.

The ``!temporary`` / ``!incognito`` modifiers were Slack-only machinery living in
``slack/handler.py``, keyed by session key but reachable only through Slack's own
call sites and gated in the dashboard by ``sk.startswith("slack:")``. These tests
pin the hoisted core against the two things a second channel needs from it: an
answer that does not depend on the key's namespace, and a mode that survives a
restart.

Every test here is written so that reverting the guard it names turns it red —
see the per-test notes on what to break.

Reservations and the in-flight commit are in
``test_messaging_privacy_mode_reservations.py``; the keys, doubles and fixtures both
share are in ``messaging_privacy_mode_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from messaging_privacy_mode_helpers import _isolate_trackers  # noqa: F401
from messaging_privacy_mode_helpers import audits_fixture  # noqa: F401
from messaging_privacy_mode_helpers import session_map_fixture  # noqa: F401
from messaging_privacy_mode_helpers import _TG_KEY, _land_on_disk, _Recorder, _Sessions

from kiro_crew.messaging import privacy_mode
from kiro_crew.session_map import SessionMap
from kiro_crew.testing.wait import LoopHop, async_wait_until, await_until

_SLACK_KEY = "slack:1700000000.000100"


# ──────────────────────────────────────────────────────────────────────
# Trackers and the restricted predicate
# ──────────────────────────────────────────────────────────────────────
class TestTrackers:
    def test_a_non_slack_key_can_be_restricted(self):
        """The defect the hoist exists to fix.

        Mutation: narrow ``is_restricted`` back to
        ``key.startswith("slack:") and ...`` — this goes red while the Slack
        assertion below stays green, which is exactly how the bug hid.
        """
        privacy_mode.mark_incognito(_TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is True
        assert privacy_mode.is_restricted(_SLACK_KEY) is False

    def test_the_slack_answer_is_unchanged(self):
        privacy_mode.mark_temporary(_SLACK_KEY)
        assert privacy_mode.is_restricted(_SLACK_KEY) is True
        assert privacy_mode.is_temporary(_SLACK_KEY) is True
        assert privacy_mode.is_incognito(_SLACK_KEY) is False

    def test_incognito_does_not_block_reads_but_temporary_does(self):
        """The two modes are tracked apart, and that difference is the contract.

        Mutation: make ``mark_incognito`` write to the temporary tracker — red,
        because an incognito session would then stop reading memory.
        """
        privacy_mode.mark_incognito(_TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY) is True
        assert privacy_mode.is_temporary(_TG_KEY) is False

    def test_the_lru_evicts_the_least_recently_marked(self, monkeypatch):
        """Mutation: drop the ``popitem`` in ``mark`` — red (``a`` survives)."""
        monkeypatch.setattr(privacy_mode, "PRIVACY_LRU_MAX", 2)
        for key in ("a", "b", "c"):
            privacy_mode.mark_incognito(key)
        assert privacy_mode.is_incognito("a") is False
        assert privacy_mode.is_incognito("b") is True
        assert privacy_mode.is_incognito("c") is True

    def test_re_marking_refreshes_recency(self, monkeypatch):
        """Mutation: drop the ``move_to_end`` in ``mark`` — red (``a`` evicted)."""
        monkeypatch.setattr(privacy_mode, "PRIVACY_LRU_MAX", 2)
        privacy_mode.mark_incognito("a")
        privacy_mode.mark_incognito("b")
        privacy_mode.mark_incognito("a")  # a is now the newest
        privacy_mode.mark_incognito("c")
        assert privacy_mode.is_incognito("a") is True
        assert privacy_mode.is_incognito("b") is False

    def test_an_unknown_mode_is_refused_rather_than_defaulted(self):
        """Mutation: make ``_tracker`` fall back to one of the two — red.

        A typo that silently marked the wrong mode fails toward the permissive
        answer, because incognito still reads memory and temporary does not.
        """
        with pytest.raises(ValueError):
            privacy_mode.mark("private", _TG_KEY)


# ──────────────────────────────────────────────────────────────────────
# Token stripping
# ──────────────────────────────────────────────────────────────────────
class TestStripping:
    def test_a_bare_token_is_stripped_and_whitespace_collapsed(self):
        assert privacy_mode.strip_token(
            "!incognito   summarize   this", privacy_mode.MODE_INCOGNITO
        ) == ("summarize this", True)

    def test_a_token_inside_a_longer_word_is_not_a_modifier(self):
        """Mutation: drop the ``(?<!\\S)``/``(?!\\S)`` guards — red.

        Without them ``/tmp/!incognito-notes`` silently turns the session
        restricted, and a mention of the word in prose does too.
        """
        for text in ("!incognitos", "x!incognito", "path/!incognito/y"):
            assert privacy_mode.strip_token(text, privacy_mode.MODE_INCOGNITO) == (text, False)

    def test_an_absent_token_returns_the_text_untouched(self):
        """No collapse when nothing matched, so "nothing to do" is detectable."""
        assert privacy_mode.strip_token("  keep   spacing  ", privacy_mode.MODE_TEMPORARY) == (
            "  keep   spacing  ",
            False,
        )

    def test_case_is_ignored(self):
        assert privacy_mode.strip_token("!TeMpOrArY go", privacy_mode.MODE_TEMPORARY) == (
            "go",
            True,
        )

    def test_strip_tokens_reports_both_in_order(self):
        text, found = privacy_mode.strip_tokens("!incognito !temporary do it")
        assert text == "do it"
        assert found == (privacy_mode.MODE_TEMPORARY, privacy_mode.MODE_INCOGNITO)

    def test_strip_token_refuses_an_unknown_mode(self):
        with pytest.raises(ValueError):
            privacy_mode.strip_token("hello", "private")


# ──────────────────────────────────────────────────────────────────────
# apply_mode: idempotence, ordering, audit, durability
# ──────────────────────────────────────────────────────────────────────
class TestApplyMode:
    @pytest.mark.asyncio
    async def test_the_notice_and_the_audit_fire_exactly_once(self, audits):
        """Mutation: delete the ``if session_key in _tracker(mode): return False``
        early exit — red, because repeating the token would spam the channel and
        the audit log with a mode that is already on."""
        rec = _Recorder()
        first = await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            notify=rec.notify,
            on_applied=rec.on_applied,
        )
        second = await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            notify=rec.notify,
            on_applied=rec.on_applied,
        )
        assert (first, second) == (True, False)
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        assert rec.hooks == [privacy_mode.MODE_INCOGNITO]
        assert len(audits) == 1

    @pytest.mark.asyncio
    async def test_the_audit_names_the_channel(self, audits):
        """Mutation: hardcode ``source="slack"`` in the audit — red.

        The audit label is the ONE thing that differs per channel, so a Telegram
        session marked incognito must not be recorded as a Slack one.
        """
        await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", caller="U9"
        )
        assert audits[0]["operation"] == "telegram.temporary_mode"
        assert audits[0]["source"] == "telegram"
        assert audits[0]["caller"] == "U9"
        assert audits[0]["outcome"] == "allowed"
        assert audits[0]["resources"] == _TG_KEY

    @pytest.mark.asyncio
    async def test_the_session_is_already_restricted_before_the_first_await(self, audits):
        """The mark must land ahead of every await.

        Mutation: move ``mark(mode, session_key)`` below ``await notify(...)`` —
        red. A concurrent inbound message landing in that window would otherwise
        see the session as unrestricted and persist a turn the user asked to
        leave no trace.
        """
        seen: list[bool] = []

        async def _notify(_message: str) -> None:
            seen.append(privacy_mode.is_restricted(_TG_KEY))

        async def _hook(_mode: str) -> None:
            seen.append(privacy_mode.is_restricted(_TG_KEY))

        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            notify=_notify,
            on_applied=_hook,
        )
        assert seen == [True, True]

    @pytest.mark.asyncio
    async def test_the_hook_runs_before_the_notice(self, audits):
        """Slack registers the thread so the confirmation itself is in-thread.

        Mutation: swap the two awaits — red.
        """
        order: list[str] = []

        async def _notify(_message: str) -> None:
            order.append("notify")

        async def _hook(_mode: str) -> None:
            order.append("hook")

        await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY,
            _TG_KEY,
            source="telegram",
            notify=_notify,
            on_applied=_hook,
        )
        assert order == ["hook", "notify"]

    @pytest.mark.asyncio
    async def test_the_durable_flag_is_written(self, audits, session_map):
        """Mutation: delete the ``_persist`` call — red.

        This is the write the restart test below reads back.
        """
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert sm.get_flag(_TG_KEY, "incognito") is True
        await _land_on_disk(sm)

    @pytest.mark.asyncio
    async def test_a_row_that_cannot_be_written_refuses_and_publishes_nothing(self, audits):
        """Mutation: mark before ``_land``, or swallow its failure -- red.

        The durable row is the record the next boot restores from; a mark with
        no row behind it is exactly the state a restart loses. So a map that
        cannot write the row refuses the turn: no mark, one ``denied`` record,
        and the user told the message was NOT processed -- never told the mode
        is on.
        """
        sm = MagicMock(spec=SessionMap)
        sm.get_flag.return_value = False
        sm.set_flag.side_effect = OSError("read-only")
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as raised:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
            )
        assert raised.value.reason == privacy_mode.REFUSAL_PERSIST_FAILED
        assert privacy_mode.is_restricted(_TG_KEY) is False
        assert rec.notices == [privacy_mode.refusal_notice("incognito", "persist_failed")]
        assert [e["outcome"] for e in audits] == ["denied"]

    @pytest.mark.asyncio
    async def test_an_auto_attribute_stub_is_not_mistaken_for_a_session_map(self, audits):
        """``MagicMock().get_flag(...)`` is truthy for EVERY flag.

        Mutation: replace the ``isinstance`` check in ``conv_state_map`` with a
        bare ``getattr`` — red, because hydrating from the stub would mark this
        session both temporary AND incognito. Failing closed, but wrongly and
        silently.
        """
        sessions = _Sessions(MagicMock())
        assert privacy_mode.conv_state_map(sessions) is None
        privacy_mode.hydrate(sessions, _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is False


# ──────────────────────────────────────────────────────────────────────
# strip_and_apply: the single-text entry point a new channel calls
# ──────────────────────────────────────────────────────────────────────
class TestStripAndApply:
    @pytest.mark.asyncio
    async def test_a_modifier_only_message_reports_only_modifier(self, audits):
        """Mutation: return ``False`` unconditionally for *only_modifier* — red.

        The caller uses it to skip the turn; without it the model is handed the
        empty string and answers the word ``!incognito`` as if it were chat.
        """
        rec = _Recorder()
        text, only = await privacy_mode.strip_and_apply(
            "!incognito", _TG_KEY, source="telegram", notify=rec.notify
        )
        assert (text, only) == ("", True)
        assert privacy_mode.is_incognito(_TG_KEY) is True
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]

    @pytest.mark.asyncio
    async def test_the_token_never_survives_into_the_returned_text(self, audits):
        """Mutation: return the original *text* instead of the stripped one — red.

        The token is an instruction to the gateway; a prompt containing it invites
        the model to answer it.
        """
        text, only = await privacy_mode.strip_and_apply(
            "!temporary what is the weather", _TG_KEY, source="telegram"
        )
        assert text == "what is the weather"
        assert only is False
        assert privacy_mode.is_temporary(_TG_KEY) is True

    @pytest.mark.asyncio
    async def test_both_modifiers_apply_and_the_payload_survives(self, audits):
        rec = _Recorder()
        text, only = await privacy_mode.strip_and_apply(
            "!temporary !incognito summarize", _TG_KEY, source="telegram", notify=rec.notify
        )
        assert (text, only) == ("summarize", False)
        assert privacy_mode.is_temporary(_TG_KEY) and privacy_mode.is_incognito(_TG_KEY)
        assert rec.notices == [privacy_mode.NOTICE_TEMPORARY, privacy_mode.NOTICE_INCOGNITO]

    @pytest.mark.asyncio
    async def test_temporary_alone_stops_before_incognito(self, audits):
        """Ordering is load-bearing: temporary first, then stop when empty."""
        rec = _Recorder()
        text, only = await privacy_mode.strip_and_apply(
            "!temporary", _TG_KEY, source="telegram", notify=rec.notify
        )
        assert (text, only) == ("", True)
        assert privacy_mode.is_temporary(_TG_KEY) is True
        assert privacy_mode.is_incognito(_TG_KEY) is False
        assert rec.notices == [privacy_mode.NOTICE_TEMPORARY]

    @pytest.mark.asyncio
    async def test_a_plain_message_applies_nothing(self, audits):
        rec = _Recorder()
        out = await privacy_mode.strip_and_apply(
            "hello there", _TG_KEY, source="telegram", notify=rec.notify
        )
        assert out == ("hello there", False)
        assert privacy_mode.is_restricted(_TG_KEY) is False
        assert rec.notices == []
        assert audits == []


# ──────────────────────────────────────────────────────────────────────
# Durability across a restart
# ──────────────────────────────────────────────────────────────────────
class TestRestartDurability:
    @pytest.mark.asyncio
    async def test_a_mode_applied_before_a_restart_is_restored_after_it(self, audits, session_map):
        """The whole point of the durable flag.

        A second ``SessionMap`` instance over the same directory shares no memory
        with the first, so hydrating from it is the restart. The in-process
        trackers are cleared in between to model the fresh gateway.

        Mutation: make ``hydrate`` a no-op (or delete ``_persist``) — red, and
        the user's ``!incognito`` silently stops holding after any restart.
        """
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)  # the deterministic durability point

        privacy_mode.reset()  # ← the restart: process-local trackers start empty
        assert privacy_mode.is_restricted(_TG_KEY) is False

        privacy_mode.hydrate(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY) is True
        assert privacy_mode.is_temporary(_TG_KEY) is True

    def test_hydrate_without_a_session_map_is_a_noop(self):
        privacy_mode.hydrate(object(), _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is False

    def test_hydrate_leaves_an_unflagged_key_untracked(self, session_map):
        privacy_mode.hydrate(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is False


# ──────────────────────────────────────────────────────────────────────
# The inbound restore: the map row and the transcript header, stricter wins
# ──────────────────────────────────────────────────────────────────────
class TestInboundRestoreReadsRowAndHeader:
    """``hydrate_inbound`` restores from BOTH durable records, so the gate stays
    private when either one says so: a map that loads empty (its file could not
    be read) still reads a thread whose header records the mode as private.
    Nothing it does can loosen a mode."""

    @staticmethod
    def _log():
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(_TG_KEY, "user", "a persistent-era turn")
        return log

    @staticmethod
    async def _set_header(log, mode: str) -> None:
        await asyncio.to_thread(
            log.update_metadata_if, _TG_KEY, {"memory_mode": mode}, lambda _m: True
        )

    @staticmethod
    def _unreadable_map(tmp_path, session_map):
        """A restart over a map file that cannot be parsed: the map loads empty."""
        from kiro_crew.session_map import SESSION_MAP_FILENAME

        (tmp_path / SESSION_MAP_FILENAME).write_text("{not json", encoding="utf-8")
        sm = session_map()
        assert sm.privacy_flagged_entries() == {}, "premise: the map loaded empty"
        return sm

    @pytest.mark.asyncio
    async def test_an_unreadable_map_with_a_private_header_still_restricts(
        self, audits, session_map, tmp_path
    ):
        """The fail-open the header read closes.

        Mutation: drop the header read from ``hydrate_inbound`` (restore from
        the row alone) -- red, and the thread runs persistent after the restart.
        """
        log = self._log()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"

        privacy_mode.reset()  # the restart
        restarted = _Sessions(self._unreadable_map(tmp_path, session_map))
        privacy_mode.hydrate(restarted, _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is False, "premise: the row alone reads open"

        await privacy_mode.hydrate_inbound(restarted, _TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY) is True
        assert privacy_mode.is_restricted(_TG_KEY) is True

    @pytest.mark.asyncio
    async def test_the_upload_gate_refuses_on_the_header_alone(self, audits, session_map, tmp_path):
        """The same record at a gate: an unreadable map plus a temporary header
        refuses both the write side and the read side."""
        from types import SimpleNamespace

        from kiro_crew.messaging import upload_gate

        log = self._log()
        await self._set_header(log, "temporary")
        state = SimpleNamespace(sessions=_Sessions(self._unreadable_map(tmp_path, session_map)))

        def _probe(*_a, **_k):
            raise AssertionError("a channel key never reaches the persisted probe")

        assert await upload_gate.session_is_restricted(state, _TG_KEY, persisted_probe=_probe)
        privacy_mode.reset()
        assert await upload_gate.session_blocks_reads(state, _TG_KEY, persisted_probe=_probe)

    @pytest.mark.asyncio
    async def test_a_stricter_header_tightens_a_looser_row(self, audits, session_map):
        log = self._log()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        await self._set_header(log, "temporary")

        privacy_mode.reset()
        await privacy_mode.hydrate_inbound(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_temporary(_TG_KEY) is True
        assert privacy_mode.is_incognito(_TG_KEY) is True

    @pytest.mark.asyncio
    async def test_a_looser_header_does_not_loosen_a_stricter_row(self, audits, session_map):
        """Mutation: let the header REPLACE the row's flags -- red."""
        log = self._log()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        await self._set_header(log, "persistent")

        privacy_mode.reset()
        await privacy_mode.hydrate_inbound(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_temporary(_TG_KEY) is True

    @pytest.mark.asyncio
    async def test_a_completed_release_leaves_nothing_to_restore(self, audits, session_map):
        """The release restores the header before it clears the row, so after it
        neither record says the mode and the restore marks nothing."""
        self._log()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)

        privacy_mode.reset()
        await privacy_mode.hydrate_inbound(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is False

    @pytest.mark.asyncio
    async def test_the_header_read_waits_for_a_durable_sequence_in_flight(
        self, audits, session_map
    ):
        """A release restores the header and drops the mark under the session's
        lock; a read taken before it finished would re-publish the mode it took
        back. Mutation: read the header outside ``_serialized`` -- red."""
        from kiro_crew.testing.wait import until_parked

        log = self._log()
        await self._set_header(log, "incognito")
        sessions = _Sessions(session_map())
        async with privacy_mode._serialized(_TG_KEY):
            task = asyncio.ensure_future(privacy_mode.hydrate_inbound(sessions, _TG_KEY))
            await until_parked(privacy_mode._key_locks[_TG_KEY].lock)
            assert not task.done(), "the read must wait for the sequence holding the lock"
            await self._set_header(log, "persistent")  # what the release restores
        await await_until(task)
        assert privacy_mode.is_restricted(_TG_KEY) is False

    @pytest.mark.asyncio
    async def test_no_sessions_reads_no_header(self, audits):
        log = self._log()
        await self._set_header(log, "incognito")
        await privacy_mode.hydrate_inbound(None, _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is False

    @pytest.mark.asyncio
    async def test_a_dashboard_chats_private_header_stays_out_of_the_channel_trackers(
        self, audits, session_map
    ):
        """A Slack thread routed into a private dashboard chat restores the chat's
        key. That chat keeps its transcript and gates its derived reads on the
        header itself; a channel mark would make the channel skip the transcript
        for it. Mutation: read every key's header -- red."""
        from kiro_crew.history import ConversationLog

        key = "dashboard:private-chat"
        log = ConversationLog()
        log.init()
        await asyncio.to_thread(
            log.update_metadata_if, key, {"memory_mode": "incognito"}, lambda _m: True
        )
        assert log.get_metadata(key).get("memory_mode") == "incognito", "premise"

        await privacy_mode.hydrate_inbound(_Sessions(session_map()), key)
        assert privacy_mode.is_restricted(key) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_header_adds_nothing_and_the_row_still_counts(
        self, audits, session_map, monkeypatch
    ):
        from kiro_crew.history import ConversationLog

        self._log()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        privacy_mode.reset()

        def _boom(self, key):
            raise RuntimeError("header unreadable")

        monkeypatch.setattr(ConversationLog, "get_metadata", _boom)
        await privacy_mode.hydrate_inbound(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY) is True
        assert privacy_mode.is_temporary(_TG_KEY) is False


# ──────────────────────────────────────────────────────────────────────
# The transcript header: the record that outlives the session-map entry
# ──────────────────────────────────────────────────────────────────────
class TestTranscriptHeaderDurability:
    """The modifier stamps ``memory_mode`` into the transcript header -- the field
    ``is_incognito_transcript`` and every memory reader already refuse on -- so a
    transcript read never depends on the session map. The header write goes
    through the default ``ConversationLog``, so these tests read the default
    sessions directory (pinned per test by the conftest) rather than a private
    ``base_dir``."""

    @staticmethod
    def _log(seed: bool = True):
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        if seed:
            with history_mod.allow_on_loop_persist():
                log.append(_TG_KEY, "user", "a persistent-era turn")
        return log

    @pytest.mark.asyncio
    async def test_the_modifier_stamps_the_transcript_header(self, audits, session_map):
        """Mutation: delete the ``_persist_transcript_mode`` call — red.

        This is the header the consolidator's header source and the consolidate
        route's header probe read.
        """
        log = self._log()
        assert "memory_mode" not in log.get_metadata(_TG_KEY), "premise: header carries no mode"
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            sessions=_Sessions(sm),
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        # The turn written before the modifier is still there; the header, not
        # the body, is what changed.
        assert [m["content"] for m in log.read_messages(_TG_KEY)] == ["a persistent-era turn"]

    @pytest.mark.asyncio
    async def test_a_transcript_that_does_not_exist_yet_gets_the_header_first(
        self, audits, session_map
    ):
        """``!incognito`` as the thread's very first message.

        The header is upserted, so the first row a later writer appends lands
        under a header that already carries the mode. Mutation: switch the
        stamp to ``require_existing=True`` — red.
        """
        from kiro_crew import history as history_mod

        log = self._log(seed=False)
        assert not log.has_log(_TG_KEY), "premise: no transcript yet"
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY,
            _TG_KEY,
            source="telegram",
            sessions=_Sessions(sm),
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"
        with history_mod.allow_on_loop_persist():
            log.append(_TG_KEY, "user", "a later turn")
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"
        assert [m["content"] for m in log.read_messages(_TG_KEY)] == ["a later turn"]

    @pytest.mark.asyncio
    async def test_the_header_is_only_ever_tightened(self, audits, session_map):
        """``!incognito`` typed after ``!temporary`` must not re-enable reads.

        Mutation: drop the guard (write the mode unconditionally) — red.
        """
        log = self._log()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY,
            _TG_KEY,
            source="telegram",
            sessions=_Sessions(sm),
        )
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            sessions=_Sessions(sm),
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"

    @pytest.mark.asyncio
    async def test_a_header_spelled_temporary_in_mixed_case_is_not_loosened(
        self, audits, session_map
    ):
        """``Temporary`` in the header, then ``!incognito``: the header keeps the
        stricter mode it spells.

        A header is not bound by the API's validation. Compared raw, ``Temporary``
        is unknown to ``strictest`` -- weaker than any mode -- so the incognito
        stamp would overwrite it and every memory reader would then see a looser
        mode than the one written. The compare normalizes first
        (``transcript_privacy_mode``, the same rule ``is_incognito_transcript``
        applies). Mutation: compare the raw string -- the header reads
        ``incognito``.
        """
        from kiro_crew import history as history_mod
        from kiro_crew.history import transcript_privacy_mode

        log = self._log()
        with history_mod.allow_on_loop_persist():
            log.update_metadata(_TG_KEY, {"memory_mode": "Temporary"})
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            sessions=_Sessions(sm),
        )
        await _land_on_disk(sm)
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        assert header == "Temporary", "the incognito stamp overwrote a stricter header"
        assert transcript_privacy_mode(header) == "temporary"

    @pytest.mark.asyncio
    async def test_a_header_write_failure_refuses_and_publishes_nothing(
        self, audits, session_map, monkeypatch
    ):
        """Mutation: swallow the header write's failure in ``_persist_transcript_mode``
        and stamp it after the mark (the r26 shape) -- red: ``DID NOT RAISE
        PrivacyModeRefused`` (the message ran, marked and announced, under a mode
        the header did not carry).

        The header is the record the out-of-process gates read:
        ``capture_session_execution`` takes an absent ``memory_mode`` as
        ``persistent`` and MCP ``register_hook`` decides on it. So its write is
        the second half of the durable step, not a best-effort tail: when it
        fails the row is taken back, nothing is marked, and the user is told the
        message was NOT processed. Patched on the class, because the module
        builds its own ``ConversationLog``.
        """
        from kiro_crew.history import ConversationLog

        self._log()
        monkeypatch.setattr(
            ConversationLog, "update_metadata_if", MagicMock(side_effect=OSError("read-only"))
        )
        sm = session_map()
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as refused:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
            )
        await _land_on_disk(sm)
        assert refused.value.reason == privacy_mode.REFUSAL_PERSIST_FAILED
        trail = [e["outcome"] for e in audits]
        assert privacy_mode.is_restricted(_TG_KEY) is False and trail == ["denied"], (
            "the failed header write published the mode: "
            f"marked={privacy_mode.is_restricted(_TG_KEY)}, trail={trail}"
        )
        assert sm.get_flag(_TG_KEY, "incognito") is False, "the row was not taken back"
        assert sm.privacy_flagged_entries() == {}
        assert privacy_mode._pending == {}
        assert len(rec.notices) == 1 and "NOT processed" in rec.notices[0]
        assert audits[0]["resources"].startswith(
            f"private_session_refused:{privacy_mode.REFUSAL_PERSIST_FAILED}:"
        )

    @staticmethod
    def _raw_first_line(text: str):
        """A transcript whose first line is *text* -- written raw, as a foreign or
        damaged writer would leave it."""
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        path = log._path(_TG_KEY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return log

    async def _apply_against(self, log, sm, rec) -> tuple[str, BaseException | None]:
        outcome, refused = "published", None
        try:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
            )
        except privacy_mode.PrivacyModeRefused as exc:
            outcome, refused = "refused", exc
        await _land_on_disk(sm)
        return outcome, refused

    def _assert_nothing_published(
        self, log, sm, rec, audits, outcome, refused, *, header_was, reason=None
    ):
        trail = [e["outcome"] for e in audits]
        assert (
            outcome == "refused" and privacy_mode.is_restricted(_TG_KEY) is False
        ) and trail == ["denied"], (
            f"a header the writer could not stamp published the mode ({header_was}): "
            f"outcome={outcome}, marked={privacy_mode.is_restricted(_TG_KEY)}, trail={trail}, "
            f"header={log.get_metadata(_TG_KEY)!r}"
        )
        expected = reason or privacy_mode.REFUSAL_PERSIST_FAILED
        assert refused is not None and refused.reason == expected, refused and refused.reason
        assert isinstance(refused.__cause__, privacy_mode.HeaderNotRecorded)
        assert sm.get_flag(_TG_KEY, "incognito") is False, "the row was not taken back"
        assert privacy_mode._pending == {}
        assert len(rec.notices) == 1 and "NOT processed" in rec.notices[0]

    @pytest.mark.asyncio
    async def test_a_damaged_first_line_refuses_and_publishes_nothing(self, audits, session_map):
        """The transcript's first line is not JSON. ``update_metadata_if`` reports
        it unreadable and writes nothing -- answering ``False``, the same answer as
        "already carries the mode". Mutation: discard the writer's answer and skip
        the read-back (the r30 shape) -- red: the mode is marked, audited
        ``allowed`` and announced ON while the header-only gates
        (``capture_session_execution`` -> ``register_hook``) read no mode at all.
        """
        log = self._raw_first_line("this is not a JSON line\n")
        assert log.get_metadata(_TG_KEY) == {}, "premise: the header cannot be read"
        sm = session_map()
        rec = _Recorder()
        outcome, refused = await self._apply_against(log, sm, rec)
        self._assert_nothing_published(
            log, sm, rec, audits, outcome, refused, header_was="a damaged first line"
        )
        assert refused.__cause__.written is False, "the writer's own answer for an unreadable line"
        assert log._path(_TG_KEY).read_text(encoding="utf-8") == "this is not a JSON line\n"

    @pytest.mark.asyncio
    async def test_a_message_first_transcript_refuses_and_publishes_nothing(
        self, audits, session_map
    ):
        """The first line is valid JSON but a MESSAGE, not the metadata record (a
        legacy transcript written before headers). ``_update_metadata_locked``
        skips it without writing, and ``update_metadata_if`` still answers
        ``True`` -- so consuming the answer alone cannot see this one; only the
        read-back can. Mutation: trust the ``True`` (skip the read-back) -- red,
        same shape as the damaged line.
        """
        first = json.dumps({"role": "user", "content": "a legacy first turn", "ts": 1.0}) + "\n"
        log = self._raw_first_line(first)
        assert log.get_metadata(_TG_KEY) == {}, "premise: no metadata record ahead of the message"
        sm = session_map()
        rec = _Recorder()
        outcome, refused = await self._apply_against(log, sm, rec)
        self._assert_nothing_published(
            log,
            sm,
            rec,
            audits,
            outcome,
            refused,
            header_was="a message-first transcript",
            reason=privacy_mode.REFUSAL_HEADER_LEGACY,
        )
        assert refused.__cause__.written is True, "the writer claimed the write it skipped"
        assert log._path(_TG_KEY).read_text(encoding="utf-8") == first, "the message is untouched"
        # A retry can never land this stamp, so the notice names the way out
        # instead of asking for one: a new thread. (The transient persist
        # failure's "try again" would send the user round in a circle.)
        assert (
            "Start a new thread" in rec.notices[0] and "Try again" not in rec.notices[0]
        ), f"the dead-end refusal does not name its remedy: {rec.notices[0]!r}"

    @pytest.mark.asyncio
    async def test_a_failed_header_write_leaves_a_row_that_pre_existed_the_commit(
        self, audits, session_map, monkeypatch
    ):
        """After a restart the tracker is empty while the thread's row is on disk;
        a re-sent ``!incognito`` (the bare-modifier branch runs before the turn
        path's ``hydrate``) commits over that row. When its header write fails
        the rollback must put the row back as it WAS, not force it off: forcing
        it off (the r30 shape) erased the thread's one durable private record
        under a notice that said only "this message was not processed", and the
        next turns ran persistent. Mutation: roll back to ``False`` -- red.
        """
        from kiro_crew.history import ConversationLog

        log = self._log()
        sm = session_map()
        sm.set_flag(_TG_KEY, "incognito", True)
        await _land_on_disk(sm)
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        assert privacy_mode.is_restricted(_TG_KEY) is False, "premise: the tracker is empty"
        monkeypatch.setattr(
            ConversationLog, "update_metadata_if", MagicMock(side_effect=OSError("read-only"))
        )
        rec = _Recorder()
        outcome, refused = await self._apply_against(log, sm, rec)
        assert outcome == "refused" and refused is not None
        assert (
            sm.get_flag(_TG_KEY, "incognito") is True
            and session_map().get_flag(_TG_KEY, "incognito") is True
        ), (
            "a failed header write cleared a flag that pre-existed the commit: "
            f"row={sm.get_flag(_TG_KEY, 'incognito')}"
        )
        assert privacy_mode.is_restricted(_TG_KEY) is False, "nothing was published"
        assert [e["outcome"] for e in audits] == ["denied"]
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_cancellation_during_the_header_write_publishes_nothing_and_keeps_the_records(
        self, audits, session_map, monkeypatch
    ):
        """The header write is now the last await of the DURABLE step, ahead of the
        mark and the record. A gateway shutdown that cancels the modifier's task
        while it is in flight therefore publishes nothing in this process -- no
        mark, no ``allowed`` record, no notice -- while both durable records
        stand: the row landed before the write, and the write itself runs on its
        worker thread to completion (a cancellation lands on the awaiting task,
        never on the thread), so the header carries the mode too. The next
        inbound message hydrates the mark from the row. Mutation: mark and
        record before the header write (the r26 order) -- red
        (``['allowed'] == []``): a mode is announced whose header write has not
        happened.
        """
        from kiro_crew.history import ConversationLog, transcript_privacy_mode

        log = self._log()
        entered = asyncio.Event()
        release = asyncio.Event()
        real_update = ConversationLog.update_metadata_if

        loop = asyncio.get_running_loop()
        hop = LoopHop(loop)

        def _blocked_write(self_log, *args, **kwargs):
            # Runs on the worker thread ``asyncio.to_thread`` hands it to; the
            # cancellation lands on the awaiting task, not on this thread, which
            # completes the write once released.
            loop.call_soon_threadsafe(entered.set)
            hop.run(release.wait(), "waiting for release event")
            return real_update(self_log, *args, **kwargs)

        monkeypatch.setattr(ConversationLog, "update_metadata_if", _blocked_write)
        sm = session_map()
        rec = _Recorder()
        task = asyncio.ensure_future(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
            )
        )
        await await_until(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert privacy_mode.is_restricted(_TG_KEY) is False, "published under a cancelled write"
        assert [(e["operation"], e["outcome"]) for e in audits] == []
        assert rec.notices == []
        release.set()
        await async_wait_until(
            lambda: transcript_privacy_mode(log.get_metadata(_TG_KEY).get("memory_mode"))
            == "incognito"
        )
        hop.check()
        assert transcript_privacy_mode(log.get_metadata(_TG_KEY).get("memory_mode")) == "incognito"
        assert sm.get_flag(_TG_KEY, "incognito") is True, "the row landed before the write"
        assert privacy_mode._pending == {}
        privacy_mode.hydrate(_Sessions(sm), _TG_KEY)
        assert privacy_mode.is_restricted(_TG_KEY) is True, "the next hydrate restores the mark"

    @pytest.mark.asyncio
    async def test_without_sessions_no_header_is_written(self, audits):
        """In-memory only means neither record: no map flag, no header."""
        log = self._log()
        await privacy_mode.apply_mode(privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram")
        assert "memory_mode" not in log.get_metadata(_TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY) is True


# ──────────────────────────────────────────────────────────────────────
# The dashboard gates the ~30 memory mutations sit behind
# ──────────────────────────────────────────────────────────────────────
class TestDashboardGateReach:
    """``_is_restricted_session`` / ``_blocks_reads_session`` must reach a
    non-Slack channel session.

    Both used ``sk.startswith("slack:")``, which does not fail loudly — it makes
    the branch structurally UNREACHABLE for every other channel, so a
    ``telegram:...`` session the user marked incognito could never enter it and
    every dashboard mutation gated on the predicate stayed open for it.
    """

    @staticmethod
    def _state_and_request(session_key: str):
        from types import SimpleNamespace

        state = SimpleNamespace(_restricted_keys=set(), _slots={}, sessions=_Sessions(None))
        request = SimpleNamespace(headers={"X-Session-Key": session_key})
        return state, request

    def test_a_telegram_incognito_session_is_restricted(self):
        """Mutation: narrow the branch back to ``sk.startswith("slack:")`` — red
        here while the Slack case below stays green."""
        from kiro_crew.dashboard.handlers._shared import _is_restricted_session

        privacy_mode.mark_incognito(_TG_KEY)
        state, request = self._state_and_request(_TG_KEY)
        assert _is_restricted_session(state, request) is True

    def test_a_telegram_temporary_session_blocks_reads(self):
        """Mutation: narrow ``_blocks_reads_session``'s branch — red."""
        from kiro_crew.dashboard.handlers._shared import _blocks_reads_session

        privacy_mode.mark_temporary(_TG_KEY)
        state, request = self._state_and_request(_TG_KEY)
        assert _blocks_reads_session(state, request) is True

    def test_the_slack_answers_are_unchanged(self):
        from kiro_crew.dashboard.handlers._shared import (
            _blocks_reads_session,
            _is_restricted_session,
        )

        state, request = self._state_and_request(_SLACK_KEY)
        assert _is_restricted_session(state, request) is False
        assert _blocks_reads_session(state, request) is False
        privacy_mode.mark_temporary(_SLACK_KEY)
        assert _is_restricted_session(state, request) is True
        assert _blocks_reads_session(state, request) is True

    def test_an_incognito_session_still_serves_reads(self):
        """Incognito blocks writes only; widening the reach must not also widen
        what each mode means."""
        from kiro_crew.dashboard.handlers._shared import (
            _blocks_reads_session,
            _is_restricted_session,
        )

        privacy_mode.mark_incognito(_TG_KEY)
        state, request = self._state_and_request(_TG_KEY)
        assert _is_restricted_session(state, request) is True
        assert _blocks_reads_session(state, request) is False

    def test_a_non_channel_key_never_enters_the_branch(self):
        """A ``dashboard:`` key answers off its slot, never off these trackers.

        Mutation: drop the ``is_channel_session_key`` guard entirely — red,
        because a dashboard slot that shares a name with a marked channel key
        would start answering off the channel trackers.
        """
        from kiro_crew.dashboard.handlers._shared import _is_restricted_session

        key = "dashboard:chat-1"
        privacy_mode.mark_incognito(key)
        state, request = self._state_and_request(key)
        assert _is_restricted_session(state, request) is False


class TestStrictest:
    """Collapsing several requests into the one mode a shared turn can carry."""

    def test_temporary_beats_incognito_whichever_order_they_arrive(self):
        assert privacy_mode.strictest(["incognito", "temporary"]) == "temporary"
        assert privacy_mode.strictest(["temporary", "incognito"]) == "temporary"

    def test_a_single_request_and_none_at_all(self):
        assert privacy_mode.strictest(["incognito"]) == "incognito"
        assert privacy_mode.strictest([]) == ""

    def test_an_unknown_name_is_ignored_rather_than_raising(self):
        # This runs on the delivery path: refusing a turn over a mode that does not
        # exist would cost the user their message to protect them from nothing.
        assert privacy_mode.strictest(["nonsense"]) == ""
        assert privacy_mode.strictest(["nonsense", "incognito"]) == "incognito"

    def test_every_mode_declares_a_rank(self):
        """A new mode must state its strength, not inherit a silent last place.

        ``_STRICTNESS`` is deliberately a second list rather than a reuse of
        ``_MODES`` (which orders by which token to STRIP first), so this is what
        keeps the two from drifting: a mode missing here would be ranked below
        everything, which for a PRIVACY mode fails toward the permissive answer.
        """
        ranked = set(privacy_mode._STRICTNESS)
        declared = {mode for mode, _pattern in privacy_mode._MODES}
        assert declared - ranked == set(), "these modes have no strictness rank"
        assert ranked - declared == set(), "these ranks name a mode that no longer exists"


# ──────────────────────────────────────────────────────────────────────
# The private-conversation cap: refuse, never evict
# ──────────────────────────────────────────────────────────────────────
class TestPrivateConversationCap:
    """The map retains at most ``PRIVACY_ROW_CAP`` privacy rows; the bound is held
    by REFUSING the next new flag, fail-closed, never by evicting a row.

    A row is the record the channel gate hydrates from, so evicting one would
    run that thread as persistent -- the leak the flag closes. Refusing means:
    no row, no in-memory mark, one SEL ``denied`` record, the user told that the
    message was NOT processed, and ``apply_mode`` raising so the caller cannot
    run the turn with the mode silently dropped. Existing rows keep hydrating
    restricted. Mutation: drop the gate in ``SessionMap.set_flag`` -- the flag is
    written and the modifier applies.
    """

    @staticmethod
    def _fill(sm: SessionMap, count: int) -> list[str]:
        keys = [f"telegram:kirocrew:direct:{n}" for n in range(count)]
        for key in keys:
            sm.set_flag(key, privacy_mode.MODE_INCOGNITO, True)
        return keys

    @pytest.mark.asyncio
    async def test_a_new_flag_past_the_cap_is_refused_and_nothing_is_written(
        self, audits, session_map, monkeypatch
    ):
        import kiro_crew.session_map as session_map_mod

        monkeypatch.setattr(session_map_mod, "PRIVACY_ROW_CAP", 3)
        sm = session_map()
        retained = self._fill(sm, 3)
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as raised:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                caller="4242",
                sessions=_Sessions(sm),
                notify=rec.notify,
                on_applied=rec.on_applied,
            )
        assert (raised.value.mode, raised.value.session_key, raised.value.reason) == (
            "incognito",
            _TG_KEY,
            privacy_mode.REFUSAL_LIMIT,
        )
        # Nothing written, nothing marked, no hook.
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert _TG_KEY not in sm._data
        assert privacy_mode.is_incognito(_TG_KEY) is False
        assert rec.hooks == []
        # Told, in words that say the message did not run.
        assert rec.notices == [privacy_mode.refusal_notice("incognito", "limit")]
        assert "NOT processed" in rec.notices[0] and "3 conversations" in rec.notices[0]
        # One denial, the denied twin of the allowed record.
        assert audits == [
            {
                "caller": "4242",
                "operation": "telegram.incognito_mode",
                "outcome": "denied",
                "source": "telegram",
                "resources": f"private_session_refused:limit:{_TG_KEY}",
            }
        ]
        # Existing rows are untouched and still hydrate restricted after a restart.
        await _land_on_disk(sm)
        privacy_mode.reset()
        fresh = session_map()
        for key in retained:
            privacy_mode.hydrate(_Sessions(fresh), key)
            assert privacy_mode.is_incognito(key) is True
        assert fresh.privacy_flagged_entries() == {key: ["incognito"] for key in retained}

    @pytest.mark.asyncio
    async def test_tightening_a_retained_row_at_the_cap_is_not_refused(
        self, audits, session_map, monkeypatch
    ):
        """``!temporary`` on a thread already incognito adds no row: admitted."""
        import kiro_crew.session_map as session_map_mod

        monkeypatch.setattr(session_map_mod, "PRIVACY_ROW_CAP", 3)
        sm = session_map()
        retained = self._fill(sm, 3)
        rec = _Recorder()
        applied = await privacy_mode.apply_mode(
            privacy_mode.MODE_TEMPORARY,
            retained[0],
            source="telegram",
            sessions=_Sessions(sm),
            notify=rec.notify,
        )
        assert applied is True
        assert sm.privacy_flagged_entries()[retained[0]] == ["temporary", "incognito"]
        assert rec.notices == [privacy_mode.NOTICE_TEMPORARY]
        assert [e["outcome"] for e in audits] == ["allowed"]

    @pytest.mark.asyncio
    async def test_an_over_long_key_is_refused_at_the_same_gate(self, audits, session_map):
        import kiro_crew.session_map as session_map_mod

        sm = session_map()
        key = "telegram:kirocrew:direct:" + "9" * session_map_mod.PRIVACY_ROW_KEY_MAX
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as raised:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_TEMPORARY,
                key,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
            )
        assert raised.value.reason == privacy_mode.REFUSAL_KEY_TOO_LONG
        assert key not in sm._data
        assert privacy_mode.is_temporary(key) is False
        assert rec.notices == [privacy_mode.refusal_notice("temporary", "key_too_long")]
        assert "NOT processed" in rec.notices[0]
        assert [e["outcome"] for e in audits] == ["denied"]
        assert audits[0]["resources"].startswith("private_session_refused:key_too_long:")

    @pytest.mark.asyncio
    async def test_a_row_that_cannot_be_written_refuses_with_a_real_map(
        self, audits, session_map, monkeypatch
    ):
        """Every application requires the row -- the modifier on a message no less
        than the reservation ahead of a steer. A real map whose ``set_flag`` fails
        is a refusal (``persist_failed``): no mark, one denial, the user told.
        Mutation: catch the write failure and mark anyway -- the mode applies with
        no row."""
        sm = session_map()
        monkeypatch.setattr(sm, "set_flag", MagicMock(side_effect=OSError("disk full")))
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as raised:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                caller="4242",
                sessions=_Sessions(sm),
                notify=rec.notify,
            )
        assert raised.value.reason == privacy_mode.REFUSAL_PERSIST_FAILED
        assert privacy_mode.is_incognito(_TG_KEY) is False
        assert rec.notices == [privacy_mode.refusal_notice("incognito", "persist_failed")]
        assert "NOT processed" in rec.notices[0]
        assert [e["outcome"] for e in audits] == ["denied"]
        assert audits[0]["resources"] == f"private_session_refused:persist_failed:{_TG_KEY}"
        assert privacy_mode._pending == {}, "a refused (mode, key) must not stay held"

    def test_the_cap_is_the_trackers_bound(self):
        import kiro_crew.session_map as session_map_mod

        assert session_map_mod.PRIVACY_ROW_CAP == privacy_mode.PRIVACY_LRU_MAX == 10_000


class TestPublishOnlyThroughTheCommitPrimitive:
    """The structural pin for the durable-first shape, over the module's AST.

    Four heads of one family (a steer before the row, a debounced flush, the
    release path, the mark) were each closed at a call site; this pins the
    SHAPE instead, so a fifth call site cannot appear unnoticed: the durable
    write and its flush live in ``_land`` alone, the publication (``mark``)
    is reached only through ``_commit_mode`` -- after ``_land`` -- plus the
    restore-from-durable ``hydrate`` and ``hydrate_inbound`` and the two
    in-memory-only wrappers, and
    the unpublish (``_tracker(...).pop``) only through ``_release_mode``.
    """

    @staticmethod
    def _tree():
        import ast
        import inspect

        return ast.parse(inspect.getsource(privacy_mode))

    @classmethod
    def _callers(cls, matches) -> dict[str, list[int]]:
        """Top-level function name -> line numbers of the calls *matches* accepts."""
        import ast

        found: dict[str, list[int]] = {}
        for node in cls._tree().body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and matches(sub.func):
                    found.setdefault(node.name, []).append(sub.lineno)
        return found

    @staticmethod
    def _is_name(name: str):
        import ast

        return lambda func: isinstance(func, ast.Name) and func.id == name

    @staticmethod
    def _is_attr(attr: str):
        import ast

        return lambda func: isinstance(func, ast.Attribute) and func.attr == attr

    def test_the_durable_write_and_its_flush_live_in_land_alone(self):
        assert set(self._callers(self._is_attr("set_flag"))) == {"_land"}
        assert set(self._callers(self._is_attr("aflush"))) == {"_land"}

    def test_mark_is_reached_only_through_the_primitive_hydrate_and_the_wrappers(self):
        callers = self._callers(self._is_name("mark"))
        assert set(callers) == {
            "_commit_mode",
            "hydrate",
            "hydrate_inbound",
            "mark_temporary",
            "mark_incognito",
        }, (
            "a new publication site: every mode must be published by _commit_mode, "
            f"after its row is on disk -- found {sorted(callers)}"
        )

    def test_in_the_primitive_the_row_lands_before_the_mark(self):
        land = self._callers(self._is_name("_land"))
        mark = self._callers(self._is_name("mark"))
        assert set(land) == {"_commit_mode", "_release_mode"}
        assert max(land["_commit_mode"]) < min(
            mark["_commit_mode"]
        ), "_commit_mode publishes before it lands the row"

    def test_in_the_primitive_the_header_is_written_after_the_row_and_before_the_mark(self):
        """The header is the second durable record (the one the out-of-process gates
        read), so it belongs to the durable step: after the row, before the mark.
        Mutation: stamp it after the mark (the r26 order) -- red."""
        land = self._callers(self._is_name("_land"))["_commit_mode"]
        header = self._callers(self._is_name("_persist_transcript_mode"))["_commit_mode"]
        mark = self._callers(self._is_name("mark"))["_commit_mode"]
        assert (
            min(land) < min(header) < min(mark)
        ), "the header must be written after the row lands and before the mode is published"

    def test_in_the_mirror_the_header_is_restored_before_the_row_is_cleared(self):
        """The release loosens the records in the OPPOSITE order to the commit --
        the header, then the row, then the mark -- so a sequence that stops
        anywhere leaves the row, the record the next boot hydrates from, still
        saying the mode. The header ops are handed to ``to_thread``; the clear is
        the ``_land`` call; the mark's drop is ``_tracker(...).pop``. Mutation:
        clear the row first (the earlier order) -- red."""
        import ast

        header = self._callers(self._is_attr("to_thread"))["_release_mode"]
        land = self._callers(self._is_name("_land"))["_release_mode"]

        def _tracker_pop(func) -> bool:
            return (
                isinstance(func, ast.Attribute)
                and func.attr == "pop"
                and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "_tracker"
            )

        mark = self._callers(_tracker_pop)["_release_mode"]
        assert (
            min(header) < min(land) < min(mark)
        ), "the release must restore the header before it clears the row, and drop the mark last"

    def test_the_primitive_and_its_mirror_have_exactly_these_callers(self):
        import ast

        assert set(self._callers(self._is_name("_commit_mode"))) == {"apply_mode", "reserve"}
        assert set(self._callers(self._is_name("_release_mode"))) == {"release"}
        # The stamp is the commit's second durable step and, in the mirror, the
        # re-stamp of a header the release loosened ahead of a clear that then
        # failed -- the same tighten-only write with the same read-back.
        assert set(self._callers(self._is_name("_persist_transcript_mode"))) == {
            "_commit_mode",
            "_release_mode",
        }
        # The header write is handed to ``to_thread`` rather than called, so it is
        # pinned by attribute reference.
        header_writers = {
            node.name
            for node in self._tree().body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(sub, ast.Attribute) and sub.attr == "update_metadata_if"
                for sub in ast.walk(node)
            )
        }
        assert header_writers == {"_persist_transcript_mode", "_release_mode"}

        def _tracker_pop(func) -> bool:
            return (
                isinstance(func, ast.Attribute)
                and func.attr == "pop"
                and isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "_tracker"
            )

        assert set(self._callers(_tracker_pop)) == {"_release_mode"}

    def test_the_confirmation_has_exactly_two_producers_both_after_the_step(self):
        """The "mode ON" text is built at two sites only: the primitive's last step
        (a plain application, ``announce=True``) and ``_announce_deferred`` -- the
        reservation's confirmation, sent by whoever commits the group (the steer
        that landed, or a plain message that ran under it). No site announces a
        mode ahead of the step it protects: the r31 shape told the user "ON" at
        reservation time and stayed silent when the steer failed."""
        assert self._callers(self._is_name("_notice")).keys() == {
            "_commit_mode",
            "_announce_deferred",
        }

    def test_the_pending_registry_is_entered_and_retired_only_by_the_group_primitives(self):
        """``_pending`` -- the held (mode, key) groups -- is written by four
        functions: entered in ``_enter`` alone, retired by ``_settle`` (a failed or
        holder-less application), ``commit`` and ``release`` (the last holder), and
        emptied by ``reset``. A pop anywhere else would drop a hold under a message."""
        import ast

        writes: dict[str, set[str]] = {}
        for node in self._tree().body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and any(
                    ast.unparse(t).startswith("_pending[") for t in sub.targets
                ):
                    writes.setdefault("enter", set()).add(node.name)
                if isinstance(sub, ast.Call) and ast.unparse(sub.func) in (
                    "_pending.pop",
                    "_pending.clear",
                    "_pending.popitem",
                ):
                    writes.setdefault(ast.unparse(sub.func), set()).add(node.name)
        assert writes == {
            "enter": {"_enter"},
            "_pending.pop": {"_settle", "commit", "release"},
            "_pending.clear": {"reset"},
        }, writes

    def test_no_module_outside_privacy_mode_publishes_a_mode(self):
        """Across ``src/``: no call of the publication functions and no direct write
        of a privacy flag outside this module. The Slack handler's ``_mark_*``
        names are aliases for tests, never called in production code."""
        import re
        from pathlib import Path

        import kiro_crew

        root = Path(kiro_crew.__file__).resolve().parent
        publish = re.compile(
            r"(privacy_mode\.mark\(|\bmark_temporary\(|\bmark_incognito\(|_mark_temporary\(|_mark_incognito\()"
        )
        flag_write = re.compile(
            r"set_flag\([^)]*(MODE_TEMPORARY|MODE_INCOGNITO|\"temporary\"|\"incognito\")[^)]*,\s*True\)"
        )
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "privacy_mode.py" and path.parent.name == "messaging":
                continue
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if publish.search(line) or flag_write.search(line):
                    offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
        assert offenders == [], "\n".join(offenders)


class TestAHeaderAlreadyAtTheModeIsNotRewritten:
    """The stamp is tighten-only AND write-free when the header already records
    the mode: a restart re-applies the modifier (the trackers start empty), and
    the startup stamp visits every flagged row on every boot -- neither may
    rewrite an unchanged header. Mutation: ``needs_tightening`` without the
    equality short-circuit -- the second application writes again."""

    @pytest.mark.asyncio
    async def test_reapplying_after_a_restart_writes_the_header_once(
        self, audits, session_map, monkeypatch
    ):
        from kiro_crew.history import ConversationLog

        log = TestTranscriptHeaderDurability._log()
        writes: list[str] = []
        real_write = ConversationLog._update_metadata_locked

        def _counting(self, key, fields):
            writes.append(key)
            return real_write(self, key, fields)

        monkeypatch.setattr(ConversationLog, "_update_metadata_locked", _counting)
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert writes == [_TG_KEY], "premise: the first application stamps the header"
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        privacy_mode.reset()  # the restart: the trackers start empty, the map is on disk
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert writes == [_TG_KEY], "a header already recording the mode was rewritten"

    @pytest.mark.parametrize(
        ("current", "mode", "expected"),
        [
            ("", "incognito", True),
            ("incognito", "temporary", True),
            ("incognito", "incognito", False),
            ("temporary", "incognito", False),
            ("temporary", "temporary", False),
        ],
    )
    def test_needs_tightening(self, current, mode, expected):
        assert privacy_mode.needs_tightening(current, mode) is expected
