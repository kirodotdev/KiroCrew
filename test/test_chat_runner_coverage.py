"""Coverage tests for ``kiro_crew.dashboard.chat_runner``.

The module's happy paths are well covered by the existing chat suites
(``test_dashboard_approval``, ``test_dashboard_chat``, ``test_eager_spawn``,
``test_goal_command`` …). What this suite targets instead is the set of
*defensive* branches those suites never reach: fail-open ``except`` arms,
deny-by-default returns, the three retry ladders that a terminal
``stop_reason`` walks, and the auto-approve rungs (trusted patterns,
read-only bash, native crew) that sit between the interactive prompt and the
session-trust flag.

Two harnesses are used and the choice between them is deliberate:

* Pure helpers are called directly. They take plain dicts / mocks, so a unit
  call reaches the branch with no turn machinery at all.
* Branches that only exist inside ``_run_chat`` are driven through a real
  turn, with a mocked provider whose ``stream()`` yields a scripted list of
  ``LLMEvent``s. That is the same shape ``test_dashboard_approval`` uses, so
  the assertions run against production dispatch rather than a re-implemented
  copy of it.

The suite is split by topic. This file keeps the ``_run_chat`` turns that read the
transcript off the loop, abort quietly on shutdown, record a start failure and count
WakaTime coding time, and the blocked-link card records. The other topics are in the
``test_chat_runner_coverage_*.py`` files beside it: ``_turn_start`` (the turn-start
fold, the app-slot dispatch guard, prompt context order, pending context, and prompt
and skill expansion), ``_output`` (metrics, snapshots, OAuth banners, channel
mirroring, segment flush and native subagent cards), ``_trust`` (hook verdicts, trust
predicates and command patterns), ``_approval`` (the auto-approve rungs and the
approval window), ``_pending`` (pending resets and discards, eager spawn, prefetch and
steer), ``_queue`` (the queue drain and synthesis), ``_commands`` (model guards,
local commands and the recovery ladders) and ``_merge_cards`` (the merge cards a turn
takes from its session's other slots and carries through a verbatim replay). The
shared builders are in ``chat_runner_coverage_helpers.py``; this module re-exports the
ones other suites import from it.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import (  # noqa: F401
    _coding_tool_call,
    _coding_tool_result,
    _complete,
    _drive,
    _errors,
    _permission,
    _quiet_sel,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
    _statusless_coding_tool_call,
)
from redaction_structured_cases import link_token

from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET, github_classic, synth


class TestPromptSubmitTranscriptRead:
    """The canonical replay's transcript read must not run on the loop."""

    @pytest.mark.asyncio
    async def test_disk_count_read_runs_off_the_loop_thread(self, tmp_path, monkeypatch):
        state, client = _runner_state(tmp_path)
        _set_stream(client, [_complete()])
        slot = _slot()
        # A non-empty in-memory window is what arms the probe; without it the
        # branch holding the read is skipped and the test would pass vacuously.
        slot.append("user", "an earlier turn", "msg msg-u")
        seen: list[int] = []
        real_read = ConversationLog.read_messages_chained

        def recording(self, key):  # noqa: ANN001 -- test double
            seen.append(threading.get_ident())
            return real_read(self, key)

        monkeypatch.setattr(ConversationLog, "read_messages_chained", recording)

        await _drive(state, slot, "hello")

        assert seen, (
            "no transcript read happened on the prompt-submit path -- this test "
            "no longer exercises the re-injection probe and would pass vacuously"
        )
        assert threading.get_ident() not in seen, (
            "the re-injection probe read the transcript on the event-loop thread; "
            "it must go through asyncio.to_thread"
        )


# ── SessionClosingError shutdown race ─────────────────────────────────────


class TestSessionClosingQuietAbort:
    """A gateway-shutdown race during turn prep is a quiet abort, not an error.

    ``SessionClosingError`` from ``close_all`` mid ``get_or_create`` gets no error card,
    no record_failure, and no exception out of ``_run_chat``.
    """

    @pytest.mark.asyncio
    async def test_get_or_create_closing_appends_no_error_card(self, tmp_path):
        from kiro_crew.session import SessionClosingError

        state, _client = _runner_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(
            side_effect=SessionClosingError(
                "SessionManager began closing during provider startup; "
                "refusing to register a session behind the shutdown snapshot"
            )
        )
        slot = _slot()

        await _drive(state, slot, "hello")

        assert _errors(slot) == [], (
            "a shutdown race during session creation must not surface an "
            "error card in the chat slot"
        )
        state.sessions.record_failure.assert_not_awaited()


class TestSessionStartFailureRowKind:
    """A session start that timed out leaves a row the Continue guard can count.

    ``record_failure`` needs a registered session, which a failed start lacks, so the
    ``session_start_failed`` row kind records it, decided from the exception's tag.
    """

    @pytest.mark.asyncio
    async def test_shared_runtime_start_timeout_row_carries_the_kind(self, tmp_path):
        from kiro_crew.acp.runtime import AcpSessionStartTimeout
        from kiro_crew.dashboard.chat_utils import SESSION_START_FAILED_KIND

        state, _client = _runner_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(
            side_effect=AcpSessionStartTimeout(
                "Request session/new timed out after 90s "
                "(4/4 session-injected MCP server(s) reported)",
                collector=None,
            )
        )
        slot = _slot()

        await _drive(state, slot, "hello")

        error = next(row for row in slot.messages if row["role"] == "error")
        assert "timed out after 90s" in error["content"]  # the prose is untouched
        assert error["meta"]["kind"] == SESSION_START_FAILED_KIND

    @pytest.mark.asyncio
    async def test_an_untagged_failure_keeps_a_plain_error_row(self, tmp_path):
        state, _client = _runner_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("something else"))
        slot = _slot()

        await _drive(state, slot, "hello")

        error = next(row for row in slot.messages if row["role"] == "error")
        assert "kind" not in (error.get("meta") or {})


class TestRunChatWakaTimeCodingAccounting:
    @staticmethod
    def _wakatime_config():
        config = chat_runner.KiroCrewConfig.load()
        config.wakatime.enabled = True
        config.wakatime.send_heartbeats = True
        return config

    @pytest.fixture
    def sent_beats(self, monkeypatch):
        """Every heartbeat payload the turn hands to the sender, in order."""
        from kiro_crew.wakatime import heartbeats

        sent: list[dict] = []
        monkeypatch.setattr(heartbeats, "_schedule_send", sent.append)
        return sent

    @staticmethod
    def _shapes(beats):
        return [{k: v for k, v in beat.items() if k != "time"} for beat in beats]

    _BEAT = {"entity": "kirocrew-session", "type": "app", "category": "ai coding", "is_write": True}

    @pytest.mark.asyncio
    async def test_statusless_coding_call_demotes_when_permission_is_denied(
        self, tmp_path, sent_beats
    ):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-denied"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _deny_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("rejected")

        state.push_slots_update.side_effect = _deny_when_registered
        with patch.object(
            chat_runner.KiroCrewConfig,
            "load",
            return_value=self._wakatime_config(),
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_denied_coding_call_emits_no_cadence_heartbeat(self, tmp_path, sent_beats):
        # F1 regression: the cadence beat fires only on CONFIRMED coding, never at
        # pre-decision dispatch, so a denied write emits neither the attribution
        # beat nor a cadence beat — no is_write ai-coding row for work that never
        # ran.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-denied-cad"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _deny_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("rejected")

        state.push_slots_update.side_effect = _deny_when_registered
        with patch.object(
            chat_runner.KiroCrewConfig,
            "load",
            return_value=self._wakatime_config(),
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_statusless_coding_call_counts_after_permission_is_approved(
        self, tmp_path, sent_beats
    ):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-approved"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_cadence"),
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_statusless_coding_call_without_permission_stays_counted(
        self, tmp_path, sent_beats
    ):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])

        config = self._wakatime_config()
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=config,
            ),
            patch.object(
                chat_runner, "note_coding_activity", wraps=chat_runner.note_coding_activity
            ) as note_activity,
            patch.object(chat_runner, "note_coding_cadence"),
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_not_awaited()
        assert note_activity.call_args.kwargs["config"] is config
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_heartbeat_project_is_the_turn_start_snapshot(self, tmp_path):
        """A project picked while the turn streams does not relabel its heartbeat.

        The pick lands as the session registers (the route is free to write
        slot.project from then on); the heartbeat still names the project the
        turn prepared on and runs in.
        """
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        start = tmp_path / "start-project"
        picked = tmp_path / "picked-mid-turn"
        start.mkdir()
        picked.mkdir()
        slot.project = str(start)

        async def _claim(*_args, **_kwargs):
            slot.project = str(picked)
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_claim)
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        note_activity.assert_called_once()
        assert note_activity.call_args.args[0] == str(start)
        assert slot.project == str(picked)

    @pytest.mark.asyncio
    async def test_many_auto_approved_coding_calls_keep_one_turn_bit(self, tmp_path, sent_beats):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        events = [_statusless_coding_tool_call(f"tc-wt-{i}") for i in range(500)]
        _set_stream(client, [*events, _complete()])

        config = self._wakatime_config()
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=config,
            ),
            patch.object(
                chat_runner, "note_coding_activity", wraps=chat_runner.note_coding_activity
            ) as note_activity,
            patch.object(chat_runner, "note_coding_cadence"),
        ):
            await _drive(state, slot)

        # Per-call ids are not retained: any number of successful coding calls
        # collapses to one bounded bit and one turn-level heartbeat.
        assert note_activity.call_args.kwargs["config"] is config
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_auto_approved_coding_call_emits_one_cadence_heartbeat(
        self, tmp_path, sent_beats
    ):
        # Duration accrual for trusted tools: an auto-approved coding call never
        # reaches a permission decision, so _wt_note_approved never fires. Its
        # completed result frame is the point past gating where it has run, so a
        # cadence beat fires there — a turn of pre-authorized coding tools
        # accrues intra-turn duration like a prompted one, not just one
        # end-of-turn instant.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-auto"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _coding_tool_result(tool_call_id),
                _complete(),
            ],
        )

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_not_awaited()
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_many_auto_approved_coding_results_emit_one_throttled_cadence(
        self, tmp_path, sent_beats
    ):
        # The throttle: several auto-approved coding calls completing inside one
        # turn (well under CADENCE_MIN_INTERVAL_SECS apart) collapse to a single
        # cadence beat, matching WakaTime's coalescing bucket, instead of one
        # beat per completed tool.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        pairs = []
        for i in range(5):
            tcid = f"tc-cad-burst-{i}"
            pairs.append(_coding_tool_call(tcid))
            pairs.append(_coding_tool_result(tcid))
        _set_stream(client, [*pairs, _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_approved_coding_call_emits_one_cadence_heartbeat(self, tmp_path, sent_beats):
        # Duration accrual: an approved coding call emits a cadence heartbeat so
        # WakaTime sees a mid-turn timestamp and accrues wall-clock duration,
        # instead of a long turn collapsing to a single end-of-turn instant.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-approved"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_denied_pending_id_is_drained_before_a_later_decision(self, tmp_path, sent_beats):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-drained"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    request_id="req-wt-deny",
                    tool_call_id=tool_call_id,
                    tool_kind="edit",
                    is_shell=False,
                ),
                # No second tool call: if the first decision failed to drain the
                # pending id, this unrelated duplicate approval would count it.
                _permission(
                    request_id="req-wt-later",
                    tool_call_id=tool_call_id,
                    tool_kind="read",
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _decide_when_registered() -> None:
            deny = slot._approval_futures.get("req-wt-deny")
            if deny is not None and not deny.done():
                deny.set_result("rejected_once")
            approve = slot._approval_futures.get("req-wt-later")
            if approve is not None and not approve.done():
                approve.set_result("approved")

        state.push_slots_update.side_effect = _decide_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-wt-deny")
        client.approve_tool.assert_awaited_once_with("req-wt-later")
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_collapsed_coding_id_cannot_be_counted_by_unrelated_approval(
        self, tmp_path, sent_beats
    ):
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG

        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        coding_tool_call_id = "AKIA" + "IOSFODNN7EXAMPLE"
        _set_stream(
            client,
            [
                # This non-coding call records the literal redaction tag as the
                # first raw source. The distinct credential-shaped coding id
                # redacts to the same key and therefore collapses its identity.
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="Reading the file",
                    tool_call_id=REDACTED_CREDENTIAL_TAG,
                    tool_kind="read",
                    tool_name="fs_read",
                ),
                _coding_tool_call(coding_tool_call_id),
                # An unrelated approval under the collapsed key must not consume
                # and count the coding call. Its own denial follows afterward.
                _permission(
                    request_id="req-wt-unrelated",
                    tool_call_id=REDACTED_CREDENTIAL_TAG,
                    tool_kind="read",
                    is_shell=False,
                ),
                _permission(
                    request_id="req-wt-deny",
                    tool_call_id=coding_tool_call_id,
                    tool_kind="edit",
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _decide_when_registered() -> None:
            approve = slot._approval_futures.get("req-wt-unrelated")
            if approve is not None and not approve.done():
                approve.set_result("approved")
            deny = slot._approval_futures.get("req-wt-deny")
            if deny is not None and not deny.done():
                deny.set_result("rejected_once")

        state.push_slots_update.side_effect = _decide_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-wt-unrelated")
        client.reject_tool.assert_awaited_once_with("req-wt-deny")
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_restored_queued_turn_does_not_emit_heartbeat(self, tmp_path, sent_beats):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])
        slot._empty_response_retries = 2

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            _quiet_sel(),
        ):
            await chat_runner._run_chat(
                state,
                slot,
                "hello",
                _turn_provenance_restored=True,
            )
        await _settle(slot)

        client.stream.assert_called_once()
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_restored_queued_turn_emits_no_cadence_heartbeat(self, tmp_path, sent_beats):
        # Opus regression: a gateway-restart replay (restored queued prompt)
        # lost its actor stamp, so even an approved coding call must not emit a
        # cadence beat — the provenance guard keeps the user fallback from
        # misclassifying the replay as interactive.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-restored"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
        ):
            await chat_runner._run_chat(
                state,
                slot,
                "hello",
                _turn_provenance_restored=True,
            )
        await _settle(slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_mcp_name_fallback_is_not_used_by_chat_runner(self, tmp_path, sent_beats):
        # A write-named MCP tool with no edit kind must not fall back to the
        # tool name to count as coding activity: only a recognised coding kind
        # counts, so a third-party MCP that happens to be named "write" stays
        # out of the WakaTime lane.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(
            client,
            [
                _coding_tool_call(
                    tool_kind="",
                    tool_name="write",
                    mcp_server_name="third-party-mcp",
                ),
                _complete(),
            ],
        )

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
        ):
            await _drive(state, slot)

        client.stream.assert_called_once()
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_many_undecided_pending_coding_ids_stay_capped(self, tmp_path, caplog):
        # A flood of pending coding calls whose permission decision never
        # arrives must not grow the retained set without bound: past the cap the
        # key is dropped and tallied, and one summary is emitted at turn end
        # (never a line per drop). The undecided calls reached no gating denial,
        # so the turn does count as coding activity — the point of this test is
        # the bound and the single summary, not suppression.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        over_cap = chat_runner._MAX_TCID_SOURCES + 50
        events = [_coding_tool_call(f"tc-wt-pending-{i}") for i in range(over_cap)]
        _set_stream(client, [*events, _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
            caplog.at_level("WARNING", logger=chat_runner.logger.name),
        ):
            await _drive(state, slot)

        # Exactly one shed summary for the whole turn, not one line per dropped
        # call.
        shed = [
            r
            for r in caplog.records
            if r.name == chat_runner.logger.name and "wakatime pending-coding cap" in r.message
        ]
        assert len(shed) == 1
        assert "shed" in shed[0].message
        # Unguarded execution counts once for the turn.
        note_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_edit_kind_still_emits_heartbeat(self, tmp_path, sent_beats):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(
            client,
            [
                _coding_tool_call(
                    tool_kind="edit",
                    tool_name="anything",
                    mcp_server_name="third-party-mcp",
                ),
                _complete(),
            ],
        )

        config = self._wakatime_config()
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=config,
            ),
            patch.object(
                chat_runner, "note_coding_activity", wraps=chat_runner.note_coding_activity
            ) as note_activity,
        ):
            await _drive(state, slot)

        assert note_activity.call_args.kwargs["config"] is config
        assert self._shapes(sent_beats) == [self._BEAT]

    @pytest.mark.asyncio
    async def test_heartbeat_uses_project_captured_at_turn_start(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        project_at_start = str(tmp_path / "project-a")
        project_after_switch = str(tmp_path / "project-b")
        slot.project = project_at_start

        async def _events():
            yield _statusless_coding_tool_call()
            slot.project = project_after_switch
            yield _complete()

        client.stream = MagicMock(return_value=_events())
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        assert slot.project == project_after_switch
        note_activity.assert_called_once()
        assert note_activity.call_args.args[0] == project_at_start

    @pytest.mark.asyncio
    async def test_injected_turn_does_not_emit_heartbeat(self, tmp_path, sent_beats):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
        ):
            await _drive(state, slot, _turn_actor="cron")

        client.stream.assert_called_once()
        assert sent_beats == []

    @pytest.mark.asyncio
    async def test_injected_turn_emits_no_cadence_heartbeat(self, tmp_path, sent_beats):
        # The cadence beat honors the same scope as the end-of-turn beat: a
        # cron/subagent/injected (non-user) turn is outside the declared surface,
        # so even an APPROVED coding call emits no cadence beat.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-cron"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
        ):
            await _drive(state, slot, _turn_actor="cron")

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert sent_beats == []


#: Two distinct link tokens whose claims decode, the shape the plan redacts.
_LINK_A = link_token(seed=150)
_LINK_B = link_token(seed=151)

#: Fixture tokens for the glued-credential cases (made up, not real keys).
_AKIA_SAMPLE = "AKIA" + "5Q7XTR2M9LBC4WZN"
_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_PEM_END = "-----END " + "PRIVATE KEY-----"
_GLUED_PAT_A = github_classic(seed=83)
_GLUED_PAT_B = github_classic(seed=84)
#: An Anthropic key in its published form: a body of 80 to 200 base64url characters.
_GLUED_SK = "sk-ant-api03-" + synth(86, seed=85) + "-CaI4AA"


class TestSegmentFlushRecords:
    """What a segment flush saves, and the credential records it keeps.

    A URL is never replaced whole: only a credential value in it is masked, so a
    flush writes no blocked-link record.
    """

    H = "h.example-sink.net"
    KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS docs example key, split so no literal sits in the repo

    @staticmethod
    def _flush(deltas: list[str]) -> tuple[str, list[dict]]:
        """``(saved text, credential records)`` for *deltas* streamed and flushed."""
        slot = _slot()
        text = ""
        for delta in deltas:
            safe, removed = chat_runner._redact_delta(delta)
            chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
            text += safe
        return chat_runner._redact_segment(slot, text)

    @classmethod
    def _assert_saves_nothing_a_delta_hid(cls, deltas: list[str], *values: str) -> None:
        """The segment is withheld, or saved holding none of *values* and no run of
        text the streamed redaction hid. A checksum decides a GitHub token, so two
        glued ones are each read whole and the save usually needs no withholding."""
        from kiro_crew.dashboard.chat_turn import segment_flush

        content, records = cls._flush(deltas)
        if content == chat_runner._SEGMENT_UNCHECKED_TEXT:
            assert records == []
            return
        assert all(value not in content for value in values), content
        shown = "".join(chat_runner._redact_delta(delta)[0] for delta in deltas)
        per_delta, _ = segment_flush._redact_segment_text(_slot(), shown)
        assert not segment_flush._saved_text_shows_more(content, per_delta), content

    def test_two_echoed_placeholders_do_not_shelter_a_cut_link_tail(self):
        """Two quoted same-host placeholders, then a presigned link cut before
        ``&X-Amz-Security-Token``: the saved reply keeps no token or signature."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        ph = f"[REDACTED: suspicious URL to {host}]"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        cut = url.index("&X-Amz-Security-Token")
        content, _ = self._flush([f"{ph}. Link: {ph}. Link: ", url[:cut], url[cut:], " done"])
        assert "TTTT" not in content and "bbbb" not in content

    def test_a_credential_tag_spanning_query_separators_does_not_shelter_the_rest(self):
        """A delta whose credential replacement swallows ``&`` separators sits
        inside a blocked address: the token and signature after it are not saved."""
        secret = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"  # AWS docs example, split
        deltas = [
            "See https://example.com/r?",
            f"aws_secret_access_key={secret}&x=1&id={self.KEY}",
            "&X-Amz-Security-Token=" + "T" * 300 + "&X-Amz-Signature=" + "b" * 64,
            " done",
        ]
        content, _ = self._flush(deltas)
        assert "TTTT" not in content and "bbbb" not in content

    def test_a_nested_presigned_link_behind_a_long_outer_query_saves_no_tail(self):
        """An outer link ending at ``?pad=``, 210 filler characters and ``&u=``,
        a nested presigned link through its key, then its token and signature,
        each in its own delta: no token or signature is saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        nested = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        k = nested.index("&X-Amz-Security-Token")
        deltas = [
            "see https://outer.example/r?pad=",
            "p" * 210 + "&u=",
            nested[:k],
            nested[k:],
            " ok",
        ]
        content, _ = self._flush(deltas)
        assert "TTTT" not in content and "bbbb" not in content

    def test_an_unrelated_placeholder_glued_before_a_cut_link_shelters_nothing(self):
        """A placeholder for another host glued straight before a presigned
        link cut at ``&X-Amz-Security-Token``: no token or signature is saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        cut = url.index("&X-Amz-Security-Token")
        other = "[REDACTED: suspicious URL to other.example]"
        content, _ = self._flush([f"see {other}", url[:cut], url[cut:], " ok"])
        assert "TTTT" not in content and "bbbb" not in content, content

    def test_a_segment_with_no_cut_or_no_blocked_host_saves_as_main_does(self):
        """With no blocked link on a placeholder's host, the folds change
        nothing: the saved text equals one pass of both removers over it, even
        with quoted placeholders and a blocked link on another host."""
        from kiro_crew.security import redact_credentials

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        ph = f"[REDACTED: suspicious URL to {host}]"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        for text in (
            f"Link: {url.replace(host, 'other-bucket.s3.amazonaws.com')} and **{ph}**, quoted.",
            f"Quoted `{ph}`. Plain https://example.com/a?b=1 here.",
            "No links at all, just [REDACTED: credential] text.",
        ):
            once, _ = redact_credentials(text)
            content, _ = self._flush([text])
            assert content == once

    def test_many_glued_unrelated_placeholders_save_without_recursion(self):
        """1,100 glued placeholders for another host before a presigned link
        cut at ``&X-Amz-Signature``: the flush finishes and saves no signature."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        cut = url.index("&X-Amz-Signature")
        many = "[REDACTED: suspicious URL to other.example]" * 1100
        content, _ = self._flush([f"see {many}", url[:cut], url[cut:], " ok"])
        assert "bbbb" not in content

    def test_a_kept_link_keeps_its_query_after_a_masked_credential(self):
        """A kept link on another host whose credential a delta masked keeps
        the query after the tag, even when that query also appears in a blocked
        link in the same segment."""
        long = "A" * 260
        kept = "https://api.ok.example/v1?token=opaque1234567890abcdef&format=json"
        deltas = [f"bad https://evil.example/x?a={long}&format=json then ", kept, " end"]
        content, _ = self._flush(deltas)
        assert "&format=json end" in content

    def test_a_tag_inside_an_unblocked_outer_link_is_not_a_kept_link(self):
        """``https://outer.example/r?u=aws_secret_access_key: <presigned>``: the
        space ends the outer link in the raw pass, so it is kept, but the tag the
        label wrote inside it is not that kept link, and the query goes."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 60
            + "&X-Amz-Signature="
            + "b" * 40
        )
        text = f"see https://outer.example/r?u=aws_secret_access_key: {url} ok"
        for cut in (56, 81, 87, 98, 134):
            content, _ = self._flush([text[:cut], text[cut:]])
            assert "TTTT" not in content and "bbbb" not in content, (cut, content)

    def test_a_segment_too_large_for_a_raw_copy_fails_closed(self):
        """Past the raw copy's cap the flush cannot redact the segment as one
        text, so it saves the withheld notice, not the streamed pieces: a link
        cut before its key leaves no placeholder in the streamed text at all."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        for cut_before in ("&X-Amz-Signature", "?X-Amz-Credential"):
            cut = url.index(cut_before)
            with patch.object(chat_runner, "_SEGMENT_RAW_MAX_CHARS", 10):
                content, links = self._flush(["see ", url[:cut], url[cut:], " ok"])
            assert (content, links) == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_a_saved_segment_is_the_raw_copy_redacted_once(self):
        """Quoted placeholders, a kept same-host link with a masked token and a
        cut blocked link in one segment: the saved text is the raw copy through
        both removers once, so nothing next to a placeholder is lost."""
        from kiro_crew.security import redact_credentials

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        ph = f"[REDACTED: suspicious URL to {host}]"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        cut = url.index("&X-Amz-Signature")
        deltas = [
            f"Earlier {ph}was blocked; also {ph} stays. Kept https://{host}/k?token=",
            "opaque1234567890abcdef ",
            "now ",
            url[:cut],
            url[cut:],
            " end",
        ]
        once = redact_credentials("".join(deltas))[0]
        content, _ = self._flush(deltas)
        assert content == once
        assert content.startswith(f"Earlier {ph}was blocked; also {ph} stays.")
        assert "bbbb" not in content

    def test_a_hostname_shaped_like_a_key_does_not_shelter_a_cut_query(self):
        """A host whose first label is an access-key shape, its presigned link
        cut at every 5th character: no saved reply keeps the query."""
        host = f"{self.KEY}.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        for cut in range(1, len(url), 5):
            content, _ = self._flush(["see ", url[:cut], url[cut:], " ok"])
            assert "TTTT" not in content and "bbbb" not in content, (cut, content)

    def test_the_streamed_footer_reflow_is_applied_to_the_saved_copy(self):
        """A glued ``[OPTIONS:]`` footer split across deltas is saved reflowed,
        the same repair the streamed text gets."""
        from kiro_crew.constants import reflow_and_label_glued_option_marker

        raw = "Pick one.\n[OPTIONS: A | B]Anytime."
        content, _ = self._flush([raw[:18], raw[18:]])
        assert content == reflow_and_label_glued_option_marker(raw)[0] != raw

    def test_a_value_only_a_per_delta_pass_removed_withholds_the_segment(self):
        """When the whole-text pass would keep a value a per-delta pass
        replaced, the segment is withheld. The raw copy is never edited to drop
        the value: a mask changes what the next pass sees."""
        slot = _slot()
        chat_runner._accumulate_segment_raw(slot, "hello ", "hello ", ())
        tag = "[REDACTED: credential]"
        chat_runner._accumulate_segment_raw(slot, "world", tag, [("world", tag)])
        result = chat_runner._redact_segment(slot, f"hello {tag}")
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_a_kept_removed_value_fails_closed(self):
        slot = _slot()
        tag = "[REDACTED: credential]"
        chat_runner._accumulate_segment_raw(slot, "world", tag, [("world", tag)])
        with patch.object(chat_runner, "_kept_removed_values", return_value=["world"]):
            result = chat_runner._redact_segment(slot, tag)
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_a_token_repeated_inside_a_blocked_link_is_never_saved(self):
        """Token A only a per-delta pass removes, then a
        blocked link whose path repeats A and whose query holds token B. A mask
        of A in the raw copy would put a space inside the link, cut it before
        its query and save B with no record. The raw copy is never masked, so
        the segment is withheld and neither token is saved anywhere."""
        a = _LINK_A
        b = _LINK_B
        deltas = [
            "prefix.",
            a,
            f" see https://h.example/r/{a}?q=prefix.{b}&padding=" + "z" * 80,
        ]
        content, links = self._flush(deltas)
        for token in (a, b):
            assert token not in content
            assert all(token not in str(r) for r in links)

    def test_each_removed_value_is_checked_on_its_own(self):
        """A token glued to the one before it, and a PAT in the same delta: each
        value a per-delta pass removed is checked on its own, so neither is saved."""
        first = github_classic(seed=80)
        token = github_classic(seed=81)
        pat = github_classic(seed=82)
        deltas = [f"tokens: {first}", f"{token} and {pat}", " end"]
        shown, removed = chat_runner._redact_delta(deltas[1])
        assert token not in shown and pat not in shown
        assert token in [value for value, _ in removed]
        self._assert_saves_nothing_a_delta_hid(deltas, first, token, pat)

    def test_a_link_token_in_a_blocked_link_query_is_saved_in_neither_text_nor_record(self):
        """A link token after ``.`` inside a blocked link's query, split across
        deltas, is saved in neither the text nor the link's record
        (``meta.blocked_links[].url``): the whole-text pass removes it, so the
        segment saves the link's placeholder. The record check for a value only
        a per-delta pass removed is covered by the
        ``_records_hold_removed_credential`` tests below."""
        token = _LINK_A
        deltas = [f"https://{self.H}/r?q=prefix.", token, "&padding=" + "z" * 80]
        shown, removed = chat_runner._redact_delta(deltas[1])
        assert token not in shown and token in [value for value, _ in removed]
        content, links = self._flush(deltas)
        assert token not in content
        assert all(token not in str(r) for r in links)
        # Only the value is masked: the URL head stays readable.
        assert content.startswith(f"https://{self.H}/r?q=prefix.")

    def test_a_check_past_the_work_ceiling_fails_closed_without_scanning(self):
        """Tens of thousands of removed values would cost seconds of substring
        work on the event loop; past the ceiling the segment is withheld."""
        slot = _slot()
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        chat_runner._accumulate_segment_raw(slot, "hello ", "hello ", ())
        chat_runner._accumulate_segment_raw(slot, "world", tag, [("world", tag)])
        with (
            patch.object(chat_runner, "_SEGMENT_CHECK_MAX_WORK", 1),
            patch.object(chat_runner, "_kept_removed_values") as kept,
        ):
            result = chat_runner._redact_segment(slot, f"hello {tag}")
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])
        kept.assert_not_called()
        removed = {"tok": tag}
        with patch.object(chat_runner, "_SEGMENT_CHECK_MAX_WORK", 3):
            assert chat_runner._records_hold_removed_credential(removed, [{"view_command": "abcd"}])
            assert not chat_runner._records_hold_removed_credential(
                removed, [{"view_command": "abc"}]
            )

    def test_a_credential_record_quoting_a_removed_value_fails_closed(self):
        """A credential record's source command and view command quote a tool
        command; a removed credential there fails the flush closed too."""
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        token = _LINK_A
        check = chat_runner._records_hold_removed_credential
        command = f"printf prefix.{token}"
        base = {"ordinal": 0, "rule": "github_pat", "label": "", "profile_command": None}
        quoting = [
            dict(base, source={"type": "command", "command": command}, view_command=command),
            dict(base, source=None, view_command=command),
            dict(base, source={"type": "file", "path": f"/x/{token}", "section": None}),
        ]
        for record in quoting:
            assert check({token: tag}, [record])
        clean = dict(base, source={"type": "command", "command": "printenv A"}, view_command=None)
        assert not check({token: tag}, [clean])
        assert not check({token: tag}, [dict(base, rule=token, source=None, view_command=None)])

    def test_a_credential_a_glued_neighbour_cut_into_saves_no_part_of_it(self):
        """Two glued tokens cut between them: each delta removes its whole token,
        and no part of the second is saved."""
        first, second = github_classic(seed=80), github_classic(seed=81)
        self._assert_saves_nothing_a_delta_hid(["tokens: " + first, second + " end"], first, second)

    @pytest.mark.parametrize(
        "deltas",
        [
            pytest.param(
                [
                    "x " + _GLUED_PAT_A,
                    _GLUED_PAT_B + " y https://h.example-sink.net/p?aws_access_key_id=",
                    "AKIA" + "5Q7XTR2M9LBC4WZN",
                    "&note=" + _GLUED_PAT_B[-8:] + " z",
                ],
                id="an-unrelated-copy-of-the-tail",
            ),
            pytest.param(
                [
                    "x " + _GLUED_PAT_A,
                    _GLUED_PAT_B + " y",
                    " https://h.example-sink.net/p?note="
                    + _GLUED_PAT_B[3:]
                    + "&id=AKIA"
                    + "5Q7XTR2M9LBC4WZN z",
                ],
                id="a-blocked-address-quoting-the-body",
            ),
            *(
                pytest.param(
                    [
                        "x " + _GLUED_PAT_A,
                        _GLUED_PAT_B + " y",
                        f" https://h.example-sink.net/p?note={quoted}&id={_AKIA_SAMPLE} z",
                    ],
                    id=f"a-blocked-address-quoting-{name}",
                )
                for name, quoted in (
                    ("nothing", ""),
                    ("a-glued-token", "Q" + _GLUED_PAT_B),
                    ("a-suffixed-token", _GLUED_PAT_B + "Q"),
                    ("a-prefixed-body", "Q" + _GLUED_PAT_B[3:]),
                    ("a-suffixed-body", _GLUED_PAT_B[3:] + "z"),
                )
            ),
            pytest.param(
                [
                    "Words p ss w0rd cache internal 6379 ok. Key: " + _GLUED_PAT_A,
                    "redis://:p@ss.w0rd!@cache.internal:6379 ok",
                ],
                id="a-connection-string-a-glued-token-swallowed",
            ),
            pytest.param(
                [
                    "Words ://:p ss @cache.internal:6379 ok. Key: " + _GLUED_PAT_A,
                    "redis://:p(ss)@cache.internal:6379 ok",
                ],
                id="a-bracketed-password-whose-pieces-the-reply-repeats",
            ),
            pytest.param(
                [
                    "x [REDACTED: " + _LINK_A,
                    f"{SAMPLE_AWS_SECRET} end",
                ],
                id="a-forged-placeholder-opener",
            ),
            pytest.param(
                [
                    "x " + _GLUED_PAT_A,
                    _GLUED_PAT_B + " y\n-----BEGIN PR",
                    "IVATE KEY-----\nhttps://" + _GLUED_PAT_B[3:] + ".example/p?id=" + _AKIA_SAMPLE,
                    "\n" + _PEM_END + "\n z",
                ],
                id="a-removed-link-domain-holding-the-body",
            ),
        ],
    )
    def test_text_a_delta_hid_and_a_glued_match_exposes_is_never_saved(self, deltas):
        """A glued neighbour, a quoted tail or a forged placeholder around a
        credential: the save holds no value a delta removed and no run the
        streamed redaction hid, or the segment is withheld."""
        self._assert_saves_nothing_a_delta_hid(deltas, _GLUED_PAT_A, _GLUED_PAT_B)

    def test_a_presigned_link_cut_anywhere_in_two_deltas_saves_no_key(self):
        """Every two-delta cut of a reply with a presigned link: the saved reply
        is either main's per-delta text redacted whole or the withheld notice,
        and never holds a run main's save hid."""
        from kiro_crew.security import redact_credentials

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential="
            f"{self.KEY}%2F20261017%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261017T000000Z"
            "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "3f9a" * 16
        )
        reply = f"Here is your link:\n\n{url}\n\nIt expires in one hour."
        for cut in range(1, len(reply) - 1, 3):
            deltas = [reply[:cut], reply[cut:]]
            content, _ = self._flush(deltas)
            # An AWS key ID is an Identifier with the structured detectors on, so
            # the key itself may stay visible; no cut may show more than main's save.
            if content != chat_runner._SEGMENT_UNCHECKED_TEXT:
                streamed = "".join(chat_runner._redact_delta(d)[0] for d in deltas)
                main = redact_credentials(streamed)[0]
                assert not chat_runner._saved_text_shows_more(content, main), cut

    def test_a_record_whose_placeholder_a_later_match_swallowed_is_dropped(self):
        """A delta blocks a link whose host is a token body; the whole text's
        PEM match then swallows the placeholder. Its record would be the only
        place the body survives, so it is dropped."""
        body = _GLUED_PAT_B[4:]
        deltas = [
            "PRIVATE KEY----- " + _PEM_END + "\n" + _GLUED_SK,
            "\n" + _PEM_BEGIN + "\nhttps://" + body + ".example/p?q=" + "z" * 210 + "\n" + _PEM_END,
        ]
        content, links = self._flush(deltas)
        assert body not in content
        assert not any(body in str(record) for record in links)

    def test_only_mains_own_text_vouches_for_a_saved_run(self):
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        link_tag = "[REDACTED: suspicious URL to x.test]"
        check = chat_runner._saved_text_shows_more
        assert not check("see it here", "see it here")
        assert not check(f"kept {link_tag} now", f"kept {link_tag}&q=1 now")
        # A blocked address main hid is not vouched for by anything.
        assert check("kept https://x.test/a?q=QQ now", f"kept {link_tag} now")
        # A placeholder domain is text the reader sees: main must show it too.
        assert check("x [REDACTED: suspicious URL to body0123.example] y", f"x {tag} y")
        # A forged opener hides nothing.
        assert check(f"x [REDACTED: hidden0123456789 {tag} end", f"x {tag} end")
        assert not check(f"{tag} x", f"{tag}{tag} x")
        # A password's punctuation keeps it one run: prose pieces cannot vouch.
        assert check(f"{tag}://:p@ss.w0rd!@h", f"p ss w0rd h {tag}{tag}h")
        assert check(f"{tag}_body0123456789 x", f"{tag}{tag} x")

    def test_a_short_piece_of_a_cut_token_value_is_saved(self):
        """A delta cut inside a ``token=`` value makes its first piece a
        credential match of its own (``pa``). That piece is not a credential
        alone, so a record field that happens to contain it (``token_parameter``)
        does not withhold the reply."""
        deltas = ["Open https://app.example.com/join?token=pa", "rtner42xyz to join."]
        content, _ = self._flush(deltas)
        assert content != chat_runner._SEGMENT_UNCHECKED_TEXT
        assert "partner42xyz" not in content and content.endswith(" to join.")

    @pytest.mark.asyncio
    async def test_a_normally_streamed_turn_never_saves_the_withheld_notice(self, tmp_path):
        """A real turn through ``_run_chat``: the slot's raw copy must hash to the
        streamed text, so the flush saves the reply. A new writer to the streamed
        text that skips the raw copy would turn every reply into the notice, and
        this goes red."""
        from kiro_crew.acp.types import EVENT_TEXT_CHUNK

        state, client = _runner_state(tmp_path)
        slot = _slot()
        url = f"https://{self.H}/x?d=" + "eyJhIjoiYiJ9" * 30
        reply = f"Here is the plan.\n\nSee [the doc]({url}) and run `make test`. Done."
        cuts = [0, 7, 30, 70, 200, len(reply)]
        chunks = [reply[a:b] for a, b in zip(cuts, cuts[1:])]
        _set_stream(
            client,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text=chunk) for chunk in chunks] + [_complete()],
        )

        await _drive(state, slot, "hello")

        saved = [row["content"] for row in slot.messages if row["role"] == "assistant"]
        assert saved and chat_runner._SEGMENT_UNCHECKED_TEXT not in saved
        assert saved[-1].startswith("Here is the plan.") and saved[-1].endswith("Done.")
        # The link is never replaced whole: with no credential in it, it stays readable.
        assert f"https://{self.H}/x?d=" in saved[-1]

    @pytest.mark.parametrize("with_blob", [False, True])
    def test_benign_replies_cut_anywhere_are_saved(self, with_blob):
        """Code with long identifiers, a plain link and prose, cut at random
        delta boundaries: every flush saves the reply, never the withheld
        notice. Accepted cost: when a cut makes a
        per-delta pass remove a piece of a base64 blob the whole text keeps,
        that segment is withheld rather than saved with a mask, so with a blob
        each flush is the saved reply or the withheld notice."""
        import random

        ident = "eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue()"
        link = "https://docs.example.com/guide/getting-started#install"
        code = "result = some_module.compute_checksum(payload_bytes, algorithm='sha256')"
        blob = "Blob: SGVsbG8gd29ybGQsIHRoaXMgaXMgYSBiZW5pZ24gYmxvYg== " if with_blob else ""
        reply = f"Use `{ident}` then open {link} .\n```python\n{code}\n```\n" f"{blob}and done."
        rng = random.Random(20805)
        for _ in range(200):
            cuts = sorted(rng.sample(range(1, len(reply)), rng.randint(1, 8)))
            bounds = [0, *cuts, len(reply)]
            content, _ = self._flush([reply[a:b] for a, b in zip(bounds, bounds[1:])])
            if with_blob and content == chat_runner._SEGMENT_UNCHECKED_TEXT:
                continue
            assert content != chat_runner._SEGMENT_UNCHECKED_TEXT, cuts
            assert content.endswith("and done."), cuts

    def test_a_redaction_pass_that_raises_fails_closed(self):
        url = "https://h.example-sink.net/only?q=" + "a" * 250
        slot = _slot()
        text = ""
        for delta in ("see ", url[:40], url[40:]):
            safe, removed = chat_runner._redact_delta(delta)
            chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
            text += safe
        with patch.object(chat_runner, "_redact_segment_text", side_effect=RuntimeError("boom")):
            result = chat_runner._redact_segment(slot, text)
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_clearing_the_streamed_text_clears_its_raw_copy(self):
        """``_cleared_segment_text`` is how a turn drops its streamed text
        mid-segment, and it takes the raw copy with it."""
        slot = _slot()
        raw = "unseen https://h.example-sink.net/x"
        chat_runner._accumulate_segment_raw(slot, raw, raw, ())
        assert slot.segment_raw_text

        assert chat_runner._cleared_segment_text(slot) == ""
        assert slot.segment_raw_text == ""
