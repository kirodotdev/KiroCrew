"""Contract tests for ``messaging/privacy_mode.py``.

The ``!temporary`` / ``!incognito`` modifiers were Slack-only machinery living in
``slack/handler.py``, keyed by session key but reachable only through Slack's own
call sites and gated in the dashboard by ``sk.startswith("slack:")``. These tests
pin the hoisted core against the two things a second channel needs from it: an
answer that does not depend on the key's namespace, and a mode that survives a
restart.

Every test here is written so that reverting the guard it names turns it red —
see the per-test notes on what to break.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.messaging import privacy_mode
from kiro_crew.session_map import SessionMap

#: A key in a namespace that is NOT Slack. The whole point of the hoist.
_TG_KEY = "telegram:kirocrew:direct:4242"
_SLACK_KEY = "slack:1700000000.000100"


@pytest.fixture(autouse=True)
def _isolate_trackers():
    """The trackers are process globals; make every test hermetic."""
    privacy_mode.reset()
    yield
    privacy_mode.reset()


@pytest.fixture()
def session_map(tmp_path, monkeypatch):
    """A real ``SessionMap`` rooted in *tmp_path*, plus a factory for a fresh one.

    A second instance reading the same directory IS the restart: it shares no
    in-memory state with the first, only the file on disk.
    """
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    return SessionMap


class _Sessions:
    """Minimal ``SessionManager`` stand-in exposing a real ``SessionMap``."""

    def __init__(self, sm: object = None) -> None:
        self._session_map = sm


async def _land_on_disk(sm: SessionMap) -> None:
    """Make *sm*'s pending state durable and leave no task behind.

    ``set_flag`` inside a running loop schedules a DEBOUNCED write, which the
    test's loop would outlive ("Task was destroyed but it is pending"). Retiring
    the task re-owes the dirty mark, so the synchronous flush after it is what
    actually lands the bytes the restart then reads.
    """
    task = getattr(sm, "_flush_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    sm.flush()


class _Recorder:
    """Collects the notices and hook calls a channel would have delivered."""

    def __init__(self) -> None:
        self.notices: list[str] = []
        self.hooks: list[str] = []
        self.restricted_at_notice: list[bool] = []

    async def notify(self, message: str) -> None:
        self.notices.append(message)

    async def on_applied(self, mode: str) -> None:
        self.hooks.append(mode)


@pytest.fixture()
def audits(monkeypatch):
    """Capture every ``log_api_access`` call the module makes."""
    events: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: events.append(kw)
    monkeypatch.setattr(privacy_mode, "sel", lambda: fake)
    return events


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

        def _blocked_write(self_log, *args, **kwargs):
            # Runs on the worker thread ``asyncio.to_thread`` hands it to; the
            # cancellation lands on the awaiting task, not on this thread, which
            # completes the write once released.
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return real_update(self_log, *args, **kwargs)

        loop = asyncio.get_running_loop()
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
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert privacy_mode.is_restricted(_TG_KEY) is False, "published under a cancelled write"
        assert [(e["operation"], e["outcome"]) for e in audits] == []
        assert rec.notices == []
        release.set()
        for _ in range(200):  # the thread finishes the write it was handed
            await asyncio.sleep(0.005)
            if transcript_privacy_mode(log.get_metadata(_TG_KEY).get("memory_mode")) == "incognito":
                break
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


class TestReservations:
    """``reserve`` applies strictly ahead of an irreversible step; ``commit`` keeps
    it, ``release`` takes it back -- and only what the reservation itself applied,
    only when no other holder is pending or committed."""

    @staticmethod
    def _seed_header():
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(_TG_KEY, "user", "a persistent-era turn")
        return log

    @pytest.mark.asyncio
    async def test_release_takes_back_row_mark_and_header_and_audits_it(self, audits, session_map):
        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)
        assert not privacy_mode.is_incognito(_TG_KEY)
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert sm.privacy_flagged_entries() == {}, "the row must stop counting against the cap"
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "persistent"
        assert [e["outcome"] for e in audits] == ["allowed", "released"]

    @pytest.mark.asyncio
    async def test_release_restores_the_weaker_mode_the_header_held_before(
        self, audits, session_map
    ):
        """``incognito`` then a reserved ``temporary`` that fails: the header goes
        back to ``incognito``, the incognito flag and mark stay."""
        log = self._seed_header()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        res = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        assert privacy_mode.is_incognito(_TG_KEY) and not privacy_mode.is_temporary(_TG_KEY)
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}

    @pytest.mark.asyncio
    async def test_a_session_already_in_the_mode_is_left_alone_by_a_release(
        self, audits, session_map
    ):
        self._seed_header()
        sm = session_map()
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert [e["outcome"] for e in audits] == [
            "allowed"
        ], "nothing to take back, nothing audited"

    @pytest.mark.asyncio
    async def test_a_second_holder_is_not_loosened_by_the_firsts_failed_step(
        self, audits, session_map
    ):
        """Two modifiers on one thread reserve before either steer lands; the first
        steer fails, the second lands. The mode must survive for the second."""
        self._seed_header()
        sm = session_map()
        first = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        second = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(first, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY), "released from under the second holder"
        assert sm.get_flag(_TG_KEY, "incognito") is True
        await privacy_mode.commit(second)
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed"]

    @pytest.mark.asyncio
    async def test_when_every_holder_fails_the_last_release_takes_the_mode_back(
        self, audits, session_map
    ):
        self._seed_header()
        sm = session_map()
        first = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        second = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.release(first, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY)
        await privacy_mode.release(second, sessions=_Sessions(sm), source="telegram")
        assert not privacy_mode.is_incognito(_TG_KEY)
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed", "released"]

    @pytest.mark.asyncio
    async def test_a_committed_reservation_is_never_loosened_by_a_later_release(
        self, audits, session_map
    ):
        self._seed_header()
        sm = session_map()
        first = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        second = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await privacy_mode.commit(first)
        await privacy_mode.release(second, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed"]

    @pytest.mark.asyncio
    async def test_reserve_refuses_like_apply_mode_and_leaves_no_pending_state(
        self, audits, session_map, monkeypatch
    ):
        sm = session_map()
        monkeypatch.setattr(sm, "set_flag", MagicMock(side_effect=OSError("disk full")))
        with pytest.raises(privacy_mode.PrivacyModeRefused):
            await privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
            )
        assert privacy_mode._pending == {} and not privacy_mode.is_incognito(_TG_KEY)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_the_row_is_on_disk_before_apply_mode_returns(self, audits, session_map):
        """The one form awaits the map's write before anything is published -- mark,
        audit, header, hook, notice, return -- so a fresh map reads the row back
        with no flush of the test's own. Mutation: skip the ``aflush`` in ``_land``
        -- the fresh map reads no flag."""
        sm = session_map()
        applied = await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert applied is True
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "the row was not durable"

    @pytest.mark.asyncio
    async def test_a_failed_flush_publishes_nothing_and_refuses(
        self, audits, session_map, monkeypatch
    ):
        """The row reached the map's memory but not the disk. Nothing is published:
        no mark (``is_incognito`` False), no header stamp (the transcript header is
        as it was), no mode-on notice -- the user is told the message was NOT
        processed, one ``denied`` record is written and ``PrivacyModeRefused``
        surfaces to the caller. The flag is back out of the map's memory too, so
        a later hydrate cannot resurrect a row that never landed. Mutation: mark
        before ``_land`` (the shape this replaces) -- red on ``is_incognito``, on
        the header, and on the trail (``allowed`` ahead of ``denied``)."""
        log = self._seed_header()
        sm = session_map()
        monkeypatch.setattr(sm, "aflush", AsyncMock(side_effect=OSError("disk full")))
        rec = _Recorder()
        with pytest.raises(privacy_mode.PrivacyModeRefused) as raised:
            await privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=_Sessions(sm),
                notify=rec.notify,
                on_applied=rec.on_applied,
            )
        assert raised.value.reason == privacy_mode.REFUSAL_PERSIST_FAILED
        assert privacy_mode.is_incognito(_TG_KEY) is False, "a mark was published with no row"
        assert privacy_mode.recorded_mode(_Sessions(sm), _TG_KEY) is None
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert "memory_mode" not in log.get_metadata(_TG_KEY), "the header was stamped"
        assert rec.hooks == []
        assert rec.notices == [privacy_mode.refusal_notice("incognito", "persist_failed")]
        assert [e["outcome"] for e in audits] == ["denied"], "the trail claims an application"
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_release_whose_clear_cannot_reach_disk_retains_the_mode(
        self, audits, session_map, monkeypatch
    ):
        """The mirror of the strict apply: a release publishes -- drops the mark,
        reports ``released`` -- only after the flag's clear is on disk. When that
        write fails the mode is RETAINED, fail-closed toward private: the flag is
        back in the map, the mark never left, the header -- loosened a step
        earlier, since the row is cleared LAST -- is re-stamped with the mode, and
        one ``retained`` record reports the failure instead of ``released``; a
        restart hydrates the mode from the row. Mutation: drop the mark and report
        before the flush -- the mark is gone with the clear still owed (red:
        ``is_incognito`` False, trail ``released``); drop the re-stamp -- red, the
        header says ``persistent`` over a row and a mark that say the mode."""
        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        monkeypatch.setattr(sm, "aflush", AsyncMock(side_effect=OSError("disk full")))
        released = await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        assert privacy_mode.is_incognito(_TG_KEY), "the mark was dropped with the clear still owed"
        assert released is False
        assert sm.get_flag(_TG_KEY, "incognito") is True
        assert (
            log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        ), "the header loosened ahead of the failed clear was not re-stamped"
        assert [e["outcome"] for e in audits] == ["allowed", "retained"]
        assert audits[-1]["resources"] == f"release_failed:persist_failed:{_TG_KEY}"
        assert privacy_mode._pending == {}
        privacy_mode.reset()
        privacy_mode.hydrate(_Sessions(session_map()), _TG_KEY)
        assert privacy_mode.is_incognito(_TG_KEY), "a restart did not hydrate the retained mode"

    async def _reserve_then_release_with(self, session_map, monkeypatch, audits, writer):
        """Reserve incognito (row, header and mark all standing), then release with
        the header restore replaced by *writer*; hand back the release's verdict
        and the state the assertions read."""
        from kiro_crew.history import ConversationLog

        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito", "premise: stamped"
        assert sm.get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        monkeypatch.setattr(ConversationLog, "update_metadata_if", writer)
        released = await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        await _land_on_disk(sm)
        return log, sm, released

    def _assert_retained(self, log, sm, released, audits, session_map, *, restore_was):
        trail = [e["outcome"] for e in audits]
        row = sm.get_flag(_TG_KEY, "incognito")
        assert (
            released is False and privacy_mode.is_incognito(_TG_KEY) and row is True
        ) and trail == ["allowed", "retained"], (
            f"a header restore that did not land was published as released ({restore_was}): "
            f"released={released}, marked={privacy_mode.is_incognito(_TG_KEY)}, row={row}, "
            f"header={log.get_metadata(_TG_KEY).get('memory_mode')!r}, trail={trail}"
        )
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "incognito"
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "the row was not put back"
        assert audits[-1]["resources"] == f"release_failed:persist_failed:{_TG_KEY}"
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_release_whose_header_restore_fails_retains_the_mode(
        self, audits, session_map, monkeypatch
    ):
        """The header restore is the second half of the durable release, not a
        best-effort tail: when it RAISES, the release publishes nothing -- the
        row is put back, the mark never leaves, one ``retained`` record reports
        it -- so the session does not read released to the channel gate while
        every header-only reader still reads private. Mutation: swallow the
        restore's failure and go on to drop the mark and report ``released``
        (the r30 shape) -- red.
        """
        log, sm, released = await self._reserve_then_release_with(
            session_map, monkeypatch, audits, MagicMock(side_effect=OSError("read-only"))
        )
        self._assert_retained(
            log, sm, released, audits, session_map, restore_was="the restore raised"
        )

    @pytest.mark.asyncio
    async def test_a_release_whose_header_restore_does_not_land_retains_the_mode(
        self, audits, session_map, monkeypatch
    ):
        """The restore's writer answers ``True`` and writes nothing (its skip of a
        first line that is not the metadata record). The header still says the
        released mode while the restore's target was ``persistent``, so the
        read-back refuses the release exactly as a raise does. Mutation: trust
        the writer's answer -- red, the same shape.
        """
        log, sm, released = await self._reserve_then_release_with(
            session_map, monkeypatch, audits, MagicMock(return_value=True)
        )
        self._assert_retained(
            log,
            sm,
            released,
            audits,
            session_map,
            restore_was="the writer answered True and wrote nothing",
        )

    @pytest.mark.asyncio
    async def test_a_release_that_fails_twice_leaves_the_row_the_next_boot_hydrates_from(
        self, audits, session_map, monkeypatch
    ):
        """The double fault: the header restore RAISES and the map's next flush
        fails too. With the row cleared first, the header restore's failure put
        the row back through that failing flush, so the header and the mark said
        the mode over a row that said nothing -- and the next boot, which
        hydrates the mark from the row alone, read the private session as
        persistent while every header-only reader still read it private. With
        the header restored first, a restore that fails loosens nothing: the
        clear is never reached, the row stands, and the restart hydrates the
        mode. Mutation: clear the row ahead of the header (the earlier order) --
        red: ``hydrated=False, row=False, header='incognito'``."""
        from kiro_crew.history import ConversationLog

        log = self._seed_header()
        sm = session_map()
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=_Sessions(sm)
        )
        await _land_on_disk(sm)
        assert session_map().get_flag(_TG_KEY, "incognito") is True, "premise: the row is on disk"
        real_flush = sm.aflush
        flushes: list[int] = []

        async def _lands_once_then_fails() -> None:
            flushes.append(len(flushes))
            if len(flushes) == 1:
                await real_flush()
                return
            raise OSError("disk full")

        monkeypatch.setattr(sm, "aflush", _lands_once_then_fails)
        monkeypatch.setattr(
            ConversationLog, "update_metadata_if", MagicMock(side_effect=OSError("read-only"))
        )
        released = await privacy_mode.release(res, sessions=_Sessions(sm), source="telegram")
        assert released is False
        # The restart: process-local trackers start empty, a second map over the
        # same directory shares no memory with the first.
        privacy_mode.reset()
        privacy_mode.hydrate(_Sessions(session_map()), _TG_KEY)
        hydrated = privacy_mode.is_incognito(_TG_KEY)
        row = session_map().get_flag(_TG_KEY, "incognito")
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        assert hydrated and row is True and header == "incognito", (
            f"the release that failed twice loosened the row a restart hydrates from: "
            f"hydrated={hydrated}, row={row}, header={header!r}, flushes={len(flushes)}"
        )
        assert [e["outcome"] for e in audits] == ["allowed", "retained"]
        assert audits[-1]["resources"] == f"release_failed:persist_failed:{_TG_KEY}"

    @pytest.mark.asyncio
    async def test_two_concurrent_reservations_cannot_erase_the_committed_mode(
        self, audits, session_map, monkeypatch
    ):
        """Two ``!incognito`` on one key, the second arriving while the first's
        application is still awaiting its header write. The shared holder is
        registered before the first's first await, so the second JOINS it and waits;
        when the second's step lands (commit) and the first's fails (release), the
        committed mode survives and exactly one row exists. Mutation: register the
        holder after the application -- the second creates its own holder, commits
        and retires it, the first then registers a fresh one and its release erases
        the mode the second committed (red: ``is_incognito`` False)."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        real_stamp = privacy_mode._persist_transcript_mode

        async def _slow_stamp(session_key: str, mode: str) -> None:
            await gate.wait()  # the first application is mid-await here
            await real_stamp(session_key, mode)

        monkeypatch.setattr(privacy_mode, "_persist_transcript_mode", _slow_stamp)
        first = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        for _ in range(200):  # past the header read, into the application, up to its stamp
            await asyncio.sleep(0.005)
            if privacy_mode.is_incognito(_TG_KEY):
                break
        assert privacy_mode.is_incognito(
            _TG_KEY
        ), "premise: the key is held as restricted before the await (the mark follows the header)"
        assert not first.done(), "premise: the first application is still awaiting its stamp"

        async def _reserve_then_steer_lands() -> privacy_mode.Reservation:
            # The second modifier: its reservation returns and its steer lands at
            # once -- so it commits the moment reserve hands it back.
            res = await privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
            await privacy_mode.commit(res)
            return res

        second = asyncio.create_task(_reserve_then_steer_lands())
        await asyncio.sleep(0)
        second_waited = not second.done()  # asserted last: the ERASE is the finding
        gate.set()
        first_res, _second_res = await asyncio.gather(first, second)
        await privacy_mode.release(
            first_res, sessions=sessions, source="telegram"
        )  # the first's steer did not land
        assert privacy_mode.is_incognito(
            _TG_KEY
        ), "the committed mode was erased by the loser's release"
        assert sm.get_flag(_TG_KEY, "incognito") is True
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed"]
        assert second_waited, "the second caller must wait for the first application to settle"

    @pytest.mark.asyncio
    async def test_a_joiner_attempts_its_own_application_when_the_firsts_failed(
        self, audits, session_map, monkeypatch
    ):
        """The first holder's application is refused (the map cannot write the row);
        the joiner does not ride a failed application -- it attempts its own, which
        the same map refuses too, and no pending state is left behind."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()

        real_set_flag = sm.set_flag

        def _slow_failing_set_flag(key, flag, value):
            raise OSError("disk full")

        monkeypatch.setattr(sm, "set_flag", _slow_failing_set_flag)
        real_read = privacy_mode.asyncio.to_thread

        async def _gated_to_thread(fn, *a, **kw):
            await gate.wait()  # hold the first holder in its pre-application header read
            return await real_read(fn, *a, **kw)

        monkeypatch.setattr(privacy_mode.asyncio, "to_thread", _gated_to_thread)
        first = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        await asyncio.sleep(0)
        assert not second.done()
        gate.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(r, privacy_mode.PrivacyModeRefused) for r in results), results
        assert privacy_mode._pending == {} and not privacy_mode.is_incognito(_TG_KEY)
        monkeypatch.setattr(sm, "set_flag", real_set_flag)

    @pytest.mark.asyncio
    async def test_releasing_one_mode_keeps_the_header_of_the_other_committed_mode(
        self, audits, session_map
    ):
        """Two reservations of DIFFERENT modes on one key -- ``/temporary`` then
        ``/incognito`` -- the incognito one commits, the temporary one releases.
        The header must still record ``incognito``: a header-only reader
        (``is_incognito_transcript``, the consolidator's header source) takes it
        at its word, so a bare ``persistent`` there exposes the committed
        incognito transcript. Mutation: restore the released reservation's own
        ``header_before`` alone (the shape this replaces) -- red, the header reads
        ``persistent`` while the incognito row and mark stand."""
        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        temp = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
        )
        inco = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary", "premise"
        await privacy_mode.commit(inco)
        released = await privacy_mode.release(temp, sessions=sessions, source="telegram")
        assert released is True
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        assert header == "incognito", f"the committed incognito mode was erased: header={header!r}"
        assert privacy_mode.is_incognito(_TG_KEY) and not privacy_mode.is_temporary(_TG_KEY)
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}
        assert privacy_mode.recorded_mode(sessions, _TG_KEY) == "incognito"
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_releasing_the_weaker_mode_leaves_the_stricter_committed_header(
        self, audits, session_map
    ):
        """The other order: ``/incognito`` reserved first, ``/temporary`` second and
        committed, incognito released. The header records ``temporary`` (the
        stricter stamp) and must keep it -- the restore is guarded by "the header
        still records the released mode" AND targets the strictest claim standing,
        and either alone would do here; both are asserted."""
        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        inco = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        temp = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
        )
        await privacy_mode.commit(temp)
        assert (
            privacy_mode._restore_target(sessions, _TG_KEY, privacy_mode.MODE_INCOGNITO, None)
            == "temporary"
        )
        assert await privacy_mode.release(inco, sessions=sessions, source="telegram") is True
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary"
        assert privacy_mode.is_temporary(_TG_KEY) and not privacy_mode.is_incognito(_TG_KEY)

    def test_the_restore_target_ranks_every_claim_normalized(self, session_map):
        """``header_before`` is normalized before ranking (a mixed-case ``Temporary``
        is ``temporary``, a foreign value is nothing); a mode held in flight for
        the session counts as a claim; ``persistent`` only when nothing stands."""
        sm = session_map()
        sessions = _Sessions(sm)
        target = privacy_mode._restore_target
        assert target(sessions, _TG_KEY, "incognito", None) == "persistent"
        assert target(sessions, _TG_KEY, "incognito", "Persistent") == "persistent"
        assert target(sessions, _TG_KEY, "incognito", "Temporary") == "temporary"
        privacy_mode.mark_incognito(_TG_KEY)
        assert target(sessions, _TG_KEY, "temporary", None) == "incognito"
        privacy_mode._pending[("temporary", _TG_KEY)] = privacy_mode._Pending(
            settled=asyncio.Event()
        )
        assert target(sessions, _TG_KEY, "incognito", None) == "temporary"

    async def _reserve_both_modes(self, session_map):
        """``!temporary`` then ``!incognito`` reserved on one thread (two steers
        in flight); the header records ``temporary`` (the incognito stamp is a
        tighten-only no-op over it). Returns ``(log, sm, sessions, temp, inco)``."""
        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        temp = await privacy_mode.reserve(
            privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
        )
        inco = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        await _land_on_disk(sm)
        assert log.get_metadata(_TG_KEY).get("memory_mode") == "temporary", "premise"
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["temporary", "incognito"]}, "premise"
        return log, sm, sessions, temp, inco

    @staticmethod
    def _gate_the_restore_to(target: str, *, then_raise: Exception | None = None):
        """A ``ConversationLog.update_metadata_if`` stand-in whose write restoring
        the header to *target* blocks in its worker thread -- ``in_write`` set,
        then ``gate`` awaited -- and then runs the real write (or raises
        *then_raise*) and sets ``done``; every other write runs untouched.
        Returns ``(stand_in, in_write, gate, done)``."""
        from kiro_crew.history import ConversationLog

        real = ConversationLog.update_metadata_if
        in_write, gate, done = threading.Event(), threading.Event(), threading.Event()

        def _gated(self, session_key, patch, predicate, **kw):
            if patch.get("memory_mode") != target:
                return real(self, session_key, patch, predicate, **kw)
            in_write.set()
            gate.wait(timeout=10)
            try:
                if then_raise is not None:
                    raise then_raise
                return real(self, session_key, patch, predicate, **kw)
            finally:
                done.set()

        return _gated, in_write, gate, done

    def _assert_nothing_private_remains(self, log, sm, audits, *, after: str) -> None:
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        rows = sm.privacy_flagged_entries()
        marks = [m for m in ("temporary", "incognito") if privacy_mode.is_restricted(_TG_KEY)]
        trail = [e["outcome"] for e in audits]
        assert header == "persistent" and rows == {} and not marks, (
            f"{after}: the header still says a mode nothing on the session claims: "
            f"header={header!r}, rows={rows}, restricted={privacy_mode.is_restricted(_TG_KEY)}, "
            f"trail={trail}"
        )
        assert trail == ["allowed", "allowed", "released", "released"]
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_two_declined_steers_released_in_order_leave_no_private_header(
        self, audits, session_map
    ):
        """``!temporary`` then ``!incognito`` on one thread, both steers declined,
        released in that order. The incognito reservation read ``temporary`` off
        the header when it reserved -- that was the temporary stamp, not a claim
        of its own -- so once temporary is released its release must restore what
        the header held before EITHER: ``persistent``. Mutation: drop
        ``_reroot_claims`` -- red, the header reads ``temporary`` with no row, no
        mark and no group left on the session (the stale private header a
        header-only reader then takes at its word for the thread's life)."""
        log, sm, sessions, temp, inco = await self._reserve_both_modes(session_map)
        assert await privacy_mode.release(temp, sessions=sessions, source="telegram") is True
        assert await privacy_mode.release(inco, sessions=sessions, source="telegram") is True
        await _land_on_disk(sm)
        self._assert_nothing_private_remains(log, sm, audits, after="two releases in order")

    @pytest.mark.asyncio
    async def test_two_declined_steers_released_together_leave_no_private_header(
        self, audits, session_map, monkeypatch
    ):
        """The same two declined steers, their releases INTERLEAVED: the temporary
        release is mid-restore (its header write, targeting the incognito claim
        still standing, blocked in its thread) when the incognito release runs.
        Unserialized, the incognito release reads temporary as still standing
        (tracker and group retire only after the header write), restores nothing
        and retires; the temporary release then writes ``incognito`` over a
        header nothing claims. Under the session's lock the second release waits
        for the first's whole sequence. Mutation: drop the lock -- red, the header
        reads ``incognito`` and ``inco_done_early`` is True."""
        from kiro_crew.history import ConversationLog

        log, sm, sessions, temp, inco = await self._reserve_both_modes(session_map)
        gated, in_write, gate, _done = self._gate_the_restore_to("incognito")
        monkeypatch.setattr(ConversationLog, "update_metadata_if", gated)
        releasing_temp = asyncio.create_task(
            privacy_mode.release(temp, sessions=sessions, source="telegram")
        )
        await asyncio.wait_for(asyncio.to_thread(in_write.wait, 10), timeout=15)
        releasing_inco = asyncio.create_task(
            privacy_mode.release(inco, sessions=sessions, source="telegram")
        )
        await asyncio.sleep(0.05)
        inco_done_early = releasing_inco.done()  # asserted last: the header is the finding
        gate.set()
        results = await asyncio.gather(releasing_temp, releasing_inco)
        await _land_on_disk(sm)
        self._assert_nothing_private_remains(log, sm, audits, after="two releases interleaved")
        assert results == [True, True]
        assert not inco_done_early, "the second release ran while the first was mid-restore"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["restored", "raised"])
    async def test_a_cancellation_during_the_header_restore_completes_the_release(
        self, audits, session_map, monkeypatch, outcome
    ):
        """The release task is cancelled (a gateway shutdown) while its header
        restore is in its worker thread: the thread cannot be stopped and the
        clear is still to come, so the durable steps run on as one shielded task,
        their outcome is awaited, and the sequence ends in one of its two states
        before the cancellation propagates -- header restored, row cleared, mark
        dropped, ``released``; or nothing loosened, row and mark standing,
        ``retained``. Mutation: re-raise at the first ``CancelledError`` (the
        earlier shape) -- red on both: the header is restored while the row and
        the mark still say the mode (``restored``), and the trail records neither
        outcome. The premise pins the ORDER: while the header restore is in
        flight the row still says the mode -- the record a restart hydrates from
        is loosened last."""
        from kiro_crew.history import ConversationLog

        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        await _land_on_disk(sm)
        gated, in_write, gate, done = self._gate_the_restore_to(
            "persistent", then_raise=OSError("read-only") if outcome == "raised" else None
        )
        monkeypatch.setattr(ConversationLog, "update_metadata_if", gated)
        releasing = asyncio.create_task(
            privacy_mode.release(res, sessions=sessions, source="telegram")
        )
        await asyncio.wait_for(asyncio.to_thread(in_write.wait, 10), timeout=15)
        assert sm.get_flag(_TG_KEY, "incognito") is True, (
            "premise: the row is loosened LAST -- it still says the mode while the header "
            "restore is in flight"
        )
        releasing.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await releasing
        await asyncio.wait_for(asyncio.to_thread(done.wait, 10), timeout=15)
        await _land_on_disk(sm)
        header = log.get_metadata(_TG_KEY).get("memory_mode")
        row = sm.get_flag(_TG_KEY, "incognito")
        marked = privacy_mode.is_incognito(_TG_KEY)
        trail = [e["outcome"] for e in audits]
        if outcome == "restored":
            whole = header == "persistent" and row is False and not marked
            whole = whole and trail == ["allowed", "released"]
        else:
            whole = header == "incognito" and row is True and marked
            whole = whole and trail == ["allowed", "retained"]
        assert whole, (
            f"a cancellation mid-restore left the records split ({outcome}): "
            f"header={header!r}, row={row}, marked={marked}, trail={trail}"
        )
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_modifier_arriving_during_a_release_waits_and_lands_its_own_row(
        self, audits, session_map, monkeypatch
    ):
        """A declined steer releases its reservation; while the release awaits its
        durable clear the mark still stands (it is dropped last). A same-mode
        message arriving in that window must not run under that mark -- it would
        lose the mode when the clear landed under it -- so the group stays
        registered, ``releasing``, the request waits, and once the release has
        settled it lands a row of its own. Mutation: pop the group before the
        awaited clear (the r26 shape) -- red: the request reads "already applied"
        off the mark, returns at once, and the release then drops row, header
        and mark under it (``marked=False, row=False, applied=False``).
        """
        from kiro_crew.history import transcript_privacy_mode

        log = self._seed_header()
        sm = session_map()
        sessions = _Sessions(sm)
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        await _land_on_disk(sm)
        gate = asyncio.Event()
        in_clear = asyncio.Event()
        real_aflush = sm.aflush

        async def _slow_flush() -> None:
            in_clear.set()
            await gate.wait()
            await real_aflush()

        monkeypatch.setattr(sm, "aflush", _slow_flush)
        releasing = asyncio.create_task(
            privacy_mode.release(res, sessions=sessions, source="telegram")
        )
        await asyncio.wait_for(in_clear.wait(), timeout=5)
        assert privacy_mode.is_incognito(_TG_KEY), "premise: the mark stands during the clear"
        assert privacy_mode.is_provisional(
            privacy_mode.MODE_INCOGNITO, _TG_KEY
        ), "a release in flight is provisional: the mode may be gone in a moment"
        rec = _Recorder()
        arriving = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        await asyncio.sleep(0.01)
        request_waited = not arriving.done()  # asserted last: the lost mode is the finding
        gate.set()
        released = await releasing
        applied = await arriving
        await _land_on_disk(sm)
        marked = privacy_mode.is_incognito(_TG_KEY)
        row = sm.get_flag(_TG_KEY, "incognito")
        assert marked and row and applied is True, (
            "the concurrent modifier ran on a mark the release then dropped: "
            f"marked={marked}, row={row}, applied={applied}"
        )
        assert released is True, "the release itself still took the reservation back"
        assert sm.privacy_flagged_entries() == {_TG_KEY: ["incognito"]}
        assert transcript_privacy_mode(log.get_metadata(_TG_KEY).get("memory_mode")) == "incognito"
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["allowed", "released", "allowed"]
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        assert request_waited, "the request must wait for the release to settle, not ride the mark"

    @pytest.mark.asyncio
    async def test_is_provisional_names_a_mode_that_may_yet_be_taken_back(
        self, audits, session_map, monkeypatch
    ):
        """``is_provisional`` is what the consolidator's refusal memo asks before
        remembering a refused session: True for a reservation whose step has not
        landed and for a commit whose write is in flight, False once committed or
        when nothing is pending."""
        sm = session_map()
        sessions = _Sessions(sm)
        assert privacy_mode.is_provisional(privacy_mode.MODE_INCOGNITO, _TG_KEY) is False
        res = await privacy_mode.reserve(
            privacy_mode.MODE_INCOGNITO, _TG_KEY, source="telegram", sessions=sessions
        )
        assert privacy_mode.is_provisional(privacy_mode.MODE_INCOGNITO, _TG_KEY) is True
        assert privacy_mode.is_provisional(privacy_mode.MODE_TEMPORARY, _TG_KEY) is False
        await privacy_mode.commit(res)
        assert privacy_mode.is_provisional(privacy_mode.MODE_INCOGNITO, _TG_KEY) is False
        # A plain application is provisional for exactly the span of its write.
        gate = asyncio.Event()
        real_stamp = privacy_mode._persist_transcript_mode

        async def _slow_stamp(session_key: str, mode: str) -> None:
            await gate.wait()
            await real_stamp(session_key, mode)

        monkeypatch.setattr(privacy_mode, "_persist_transcript_mode", _slow_stamp)
        applying = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_TEMPORARY, _TG_KEY, source="telegram", sessions=sessions
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.005)
            if privacy_mode.is_provisional(privacy_mode.MODE_TEMPORARY, _TG_KEY):
                break
        assert privacy_mode.is_provisional(privacy_mode.MODE_TEMPORARY, _TG_KEY) is True
        gate.set()
        assert await applying is True
        assert privacy_mode.is_provisional(privacy_mode.MODE_TEMPORARY, _TG_KEY) is False
        assert privacy_mode.is_temporary(_TG_KEY) is True


class TestAnInFlightCommitIsHeldNotPublished:
    """Between the modifier and its row landing on disk, the key is HELD: the
    predicates answer restricted (a concurrent message runs restricted, never
    persistent), yet nothing is published -- no mark, no header, no notice -- and
    the hold vanishes with a write that fails."""

    @staticmethod
    def _gated_flush(sm, gate: asyncio.Event, *, fail: bool):
        real = sm.aflush

        async def _aflush() -> None:
            await gate.wait()
            if fail:
                raise OSError("disk full")
            await real()

        return _aflush

    @pytest.mark.asyncio
    async def test_the_key_is_restricted_while_the_row_lands_and_free_if_it_fails(
        self, audits, session_map, monkeypatch
    ):
        """Mutation: drop the ``_held`` term from the predicates -- red on the first
        block (a message arriving mid-write would run persistent); publish the mark
        ahead of the flush -- red on the second (the tracker holds a key whose row
        never landed)."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        monkeypatch.setattr(sm, "aflush", self._gated_flush(sm, gate, fail=True))
        rec = _Recorder()
        task = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.002)
            if privacy_mode._landing(privacy_mode.MODE_INCOGNITO, _TG_KEY):
                break
        assert privacy_mode._landing("incognito", _TG_KEY), "premise: the write is in flight"
        # Held, not published.
        assert privacy_mode.is_incognito(_TG_KEY) and privacy_mode.is_restricted(_TG_KEY)
        assert privacy_mode.recorded_mode(sessions, _TG_KEY) == "incognito"
        assert _TG_KEY not in privacy_mode._incognito, "the mark was published before the row"
        privacy_mode.hydrate(sessions, _TG_KEY)
        assert _TG_KEY not in privacy_mode._incognito, "hydrate published a row still landing"
        assert rec.notices == [] and audits == []
        gate.set()
        with pytest.raises(privacy_mode.PrivacyModeRefused):
            await task
        # The hold is gone with the write.
        assert not privacy_mode.is_restricted(_TG_KEY)
        assert privacy_mode.recorded_mode(sessions, _TG_KEY) is None
        assert sm.get_flag(_TG_KEY, "incognito") is False
        assert privacy_mode._pending == {}
        assert [e["outcome"] for e in audits] == ["denied"]

    @pytest.mark.asyncio
    async def test_the_mark_lands_once_the_row_is_on_disk(self, audits, session_map, monkeypatch):
        """The success half: the mark, the ``allowed`` record and the notice all
        follow the flush, and a fresh map reads the row back."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        monkeypatch.setattr(sm, "aflush", self._gated_flush(sm, gate, fail=False))
        rec = _Recorder()
        task = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.002)
            if privacy_mode._landing(privacy_mode.MODE_INCOGNITO, _TG_KEY):
                break
        assert _TG_KEY not in privacy_mode._incognito and audits == [] and rec.notices == []
        gate.set()
        assert await task is True
        assert _TG_KEY in privacy_mode._incognito
        assert session_map().get_flag(_TG_KEY, "incognito") is True
        assert [e["outcome"] for e in audits] == ["allowed"]
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        assert privacy_mode._pending == {}

    @pytest.mark.asyncio
    async def test_a_second_modifier_during_the_write_joins_and_commits_the_group(
        self, audits, session_map, monkeypatch
    ):
        """A concurrent ``apply_mode`` for the same (mode, key) finds the group and
        waits instead of re-committing: one row, one ``allowed`` record, one notice;
        and because its message runs under the mode it COMMITS the group, so a
        reservation riding the same group cannot take the mode back."""
        sm = session_map()
        sessions = _Sessions(sm)
        gate = asyncio.Event()
        monkeypatch.setattr(sm, "aflush", self._gated_flush(sm, gate, fail=False))
        rec = _Recorder()
        first = asyncio.create_task(
            privacy_mode.reserve(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.002)
            if privacy_mode._landing(privacy_mode.MODE_INCOGNITO, _TG_KEY):
                break
        second = asyncio.create_task(
            privacy_mode.apply_mode(
                privacy_mode.MODE_INCOGNITO,
                _TG_KEY,
                source="telegram",
                sessions=sessions,
                notify=rec.notify,
            )
        )
        await asyncio.sleep(0)
        assert not second.done(), "the second modifier must wait for the write in flight"
        gate.set()
        res, applied_by_second = await asyncio.gather(first, second)
        assert applied_by_second is False
        assert [e["outcome"] for e in audits] == ["allowed"]
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]
        # The plain modifier's message ran under the mode: the reservation's failed
        # step must not loosen it.
        assert await privacy_mode.release(res, sessions=sessions, source="telegram") is False
        assert privacy_mode.is_incognito(_TG_KEY) and sm.get_flag(_TG_KEY, "incognito") is True


class TestPublishOnlyThroughTheCommitPrimitive:
    """The structural pin for the durable-first shape, over the module's AST.

    Four heads of one family (a steer before the row, a debounced flush, the
    release path, the mark) were each closed at a call site; this pins the
    SHAPE instead, so a fifth call site cannot appear unnoticed: the durable
    write and its flush live in ``_land`` alone, the publication (``mark``)
    is reached only through ``_commit_mode`` -- after ``_land`` -- plus the
    restore-from-durable ``hydrate`` and the two in-memory-only wrappers, and
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
        assert set(callers) == {"_commit_mode", "hydrate", "mark_temporary", "mark_incognito"}, (
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
