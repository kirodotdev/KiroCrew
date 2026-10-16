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
steer), ``_queue`` (the queue drain and synthesis) and ``_commands`` (model guards,
local commands and the recovery ladders). The shared builders are in
``chat_runner_coverage_helpers.py``; this module re-exports the ones other suites
import from it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
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

from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent


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

    Two dashboard slots were mid ``get_or_create`` when a restart's
    ``close_all`` set ``_closing``; the allocation gate raised
    ``SessionClosingError`` (by design) but the generic terminal handler then
    logged an ERROR traceback, appended an ❌ error card to the slot, and
    recorded a spurious session failure. The dedicated arm must swallow it: no
    card, no record_failure, no exception out of ``_run_chat``.
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

    ``record_failure`` cannot count this failure: it increments a counter on the
    REGISTERED session, and a start that never answered registered none, so the
    call returns False and nothing anywhere remembers that the start failed.
    The structural ``session_start_failed`` row kind is that memory -- decided
    from the exception's tag (set at the ACP raise sites on both exception
    families), never from the prose, so the Continue endpoint and the error card
    can count consecutive failed starts without matching on message wording.
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

    @pytest.mark.asyncio
    async def test_statusless_coding_call_demotes_when_permission_is_denied(self, tmp_path):
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
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_coding_call_emits_no_cadence_heartbeat(self, tmp_path):
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
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        note_activity.assert_not_called()
        note_cadence.assert_not_called()

    @pytest.mark.asyncio
    async def test_statusless_coding_call_counts_after_permission_is_approved(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        note_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_statusless_coding_call_without_permission_stays_counted(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_not_awaited()
        note_activity.assert_called_once()
        assert note_activity.call_args.kwargs["config"] is config

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
    async def test_many_auto_approved_coding_calls_keep_one_turn_bit(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        events = [_statusless_coding_tool_call(f"tc-wt-{i}") for i in range(500)]
        _set_stream(client, [*events, _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        # Per-call ids are not retained: any number of successful coding calls
        # collapses to one bounded bit and one turn-level heartbeat.
        note_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_approved_coding_call_emits_one_cadence_heartbeat(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_not_awaited()
        note_cadence.assert_called_once()

    @pytest.mark.asyncio
    async def test_many_auto_approved_coding_results_emit_one_throttled_cadence(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        note_cadence.assert_called_once()

    @pytest.mark.asyncio
    async def test_approved_coding_call_emits_one_cadence_heartbeat(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        note_cadence.assert_called_once()

    @pytest.mark.asyncio
    async def test_denied_pending_id_is_drained_before_a_later_decision(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_collapsed_coding_id_cannot_be_counted_by_unrelated_approval(self, tmp_path):
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG

        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        coding_tool_call_id = "AKIAIOSFODNN7EXAMPLE"
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
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-wt-unrelated")
        client.reject_tool.assert_awaited_once_with("req-wt-deny")
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_restored_queued_turn_does_not_emit_heartbeat(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity") as note_activity,
            _quiet_sel(),
        ):
            await chat_runner._run_chat(
                state,
                slot,
                "hello",
                _turn_provenance_restored=True,
            )
        await _settle(slot)

        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_restored_queued_turn_emits_no_cadence_heartbeat(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await chat_runner._run_chat(
                state,
                slot,
                "hello",
                _turn_provenance_restored=True,
            )
        await _settle(slot)

        note_cadence.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_name_fallback_is_not_used_by_chat_runner(self, tmp_path):
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

        with patch.object(chat_runner, "note_coding_activity") as note_activity:
            await _drive(state, slot)

        note_activity.assert_not_called()

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
        # call — the amplified-log finding.
        shed = [r for r in caplog.records if "wakatime pending-coding cap" in r.message]
        assert len(shed) == 1
        assert "shed" in shed[0].message
        # Unguarded execution counts once for the turn.
        note_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_edit_kind_still_emits_heartbeat(self, tmp_path):
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
    async def test_injected_turn_does_not_emit_heartbeat(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot, _turn_actor="cron")

        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_injected_turn_emits_no_cadence_heartbeat(self, tmp_path):
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
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot, _turn_actor="cron")

        note_cadence.assert_not_called()


class TestBlockedLinkCardRecords:
    """What the dashboard's blocked-link card lists after a flush.

    The card lists the host's records in the saved reply, each under its own
    address, and never claims which one a chip stands for
    (``BlockedLinkCard`` in ``website/src/components/RedactionCards.tsx``).
    Counts cannot prove that a placeholder and a record are the same URL, so
    these pin what each record says about itself. They stream the deltas
    through both per-delta removers in the order the streaming loop applies
    them, then flush.
    """

    H = "h.example-sink.net"
    KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS docs example key, split so no literal sits in the repo

    @staticmethod
    def _flush(deltas: list[str]) -> tuple[str, list[dict]]:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        slot = _slot()
        text = ""
        for delta in deltas:
            safe, _ = redact_exfiltration_urls(delta)
            safe, _ = redact_credentials(safe)
            chat_runner._accumulate_segment_raw(slot, delta)
            text += safe
        redacted, links, _ = chat_runner._redact_segment(slot, text)
        return redacted, links

    def test_one_address_on_its_host_is_its_one_record(self):
        url = f"https://{self.H}/only?q=" + "a" * 250
        content, links = self._flush(["See ", url[:40], url[40:], " now."])
        assert content.count(f"[REDACTED: suspicious URL to {self.H}]") == 1
        assert [r["url"] for r in links] == [url]

    def test_glued_pair_a_credential_tag_and_a_key_split_link_list_every_record(self):
        # Two same-host long-query addresses glued across a delta, a
        # credential tag, then a same-host address whose key a delta redacted.
        g1 = f"https://{self.H}/g1?q=" + "a" * 250
        g2 = f"https://{self.H}/g2?q=" + "b" * 250
        deltas = [
            "Two links: ",
            g1,
            g2,
            " [REDACTED: credential] then ",
            f"https://{self.H}/other?k=",
            self.KEY + "&t=x",
            " end.",
        ]
        _, links = self._flush(deltas)
        assert sum(r["domain"] == self.H for r in links) > 1
        assert all(r["path"] for r in links if r["domain"] == self.H)

    @staticmethod
    def _key_split_presigned_flush() -> dict:
        """Presigned S3 addresses split at ``&X-Amz-Date`` beside same-host
        addresses whose key a delta redacted. The dashboard test
        ``MarkdownRenderer.blockedLinkCredInUrl.test.tsx`` renders this output."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        key = TestBlockedLinkCardRecords.KEY
        deltas: list[str] = []
        for i, name in enumerate(("report.csv", "invoice.pdf")):
            url = (
                f"https://{host}/{name}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={key}"
                "%2F20261008%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261008T000000Z"
                "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "a" * 64
            )
            cut = url.index("&X-Amz-Date")
            deltas += [f"Link {i + 1}: ", url[:cut], url[cut:], " . "]
        for name in ("other-a", "other-b"):
            deltas += [" plain ", f"https://{host}/{name}?X-Amz-Credential=", key + "%2Fx", " . "]
        content, links = TestBlockedLinkCardRecords._flush(deltas)
        return {"content": content, "blocked_links": links}

    def test_key_split_links_beside_presigned_starts_match_the_dashboard_fixture(self):
        out = self._key_split_presigned_flush()
        fixture = Path(__file__).resolve().parents[1] / "website/src/test/fixtures/credInUrl.json"
        assert json.loads(fixture.read_text(encoding="utf-8")) == out

    @staticmethod
    def _one_presigned_one_key_split_flush() -> dict:
        """ONE presigned S3 address split at ``&X-Amz-Date`` (a placeholder
        with no record of its own) beside ONE same-host address whose key a
        delta redacted (a record the text shows no placeholder for). The
        dashboard test ``MarkdownRenderer.blockedLinkOneOne.test.tsx`` renders
        this output."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        key = TestBlockedLinkCardRecords.KEY
        url = (
            f"https://{host}/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={key}"
            "%2F20261008%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261008T000000Z"
            "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "a" * 64
        )
        cut = url.index("&X-Amz-Date")
        deltas = [
            "Link: ",
            url[:cut],
            url[cut:],
            " . plain ",
            f"https://{host}/other-a?X-Amz-Credential=",
            key + "%2Fx",
            " . ",
        ]
        content, links = TestBlockedLinkCardRecords._flush(deltas)
        return {"content": content, "blocked_links": links}

    def test_one_placeholder_and_one_record_of_different_addresses_name_their_own(self):
        """One placeholder and one record need not be the same URL: the record
        names /other-a, not the report.csv the chip stands for. The card lists
        it under that address and never as the clicked link."""
        out = self._one_presigned_one_key_split_flush()
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        assert out["content"].count(f"[REDACTED: suspicious URL to {host}]") == 1
        assert [r["path"] for r in out["blocked_links"]] == ["/other-a"]
        fixture = (
            Path(__file__).resolve().parents[1] / "website/src/test/fixtures/blockedLinkOneOne.json"
        )
        assert json.loads(fixture.read_text(encoding="utf-8")) == out

    def test_clearing_the_streamed_text_clears_its_raw_copy(self):
        """``_cleared_segment_text`` is how a turn drops its streamed text
        mid-segment, and it takes the raw copy with it."""
        slot = _slot()
        chat_runner._accumulate_segment_raw(slot, "unseen https://h.example-sink.net/x")
        assert slot.segment_raw_text

        assert chat_runner._cleared_segment_text(slot) == ""
        assert slot.segment_raw_text == ""
