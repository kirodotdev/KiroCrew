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
from unittest.mock import MagicMock

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
    async def test_a_persist_failure_still_leaves_the_session_restricted(self, audits):
        """Mutation: let ``_persist`` re-raise instead of logging — red.

        The in-memory mark already happened, so failing the modifier here would
        tell the user privacy is off while it is on for this whole process.
        """
        sm = MagicMock(spec=SessionMap)
        sm.set_flag.side_effect = OSError("read-only")
        rec = _Recorder()
        applied = await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            _TG_KEY,
            source="telegram",
            sessions=_Sessions(sm),
            notify=rec.notify,
        )
        assert applied is True
        assert privacy_mode.is_restricted(_TG_KEY) is True
        assert rec.notices == [privacy_mode.NOTICE_INCOGNITO]

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
