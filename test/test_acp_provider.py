"""Unit tests for AcpProvider slash-command and compact routing.

claude-agent-acp does not implement the kiro-only
``_kiro.dev/commands/execute`` JSON-RPC method, so slash commands and
/compact must flow through ``session/prompt`` for that backend.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import AcpAuthRequired
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, AcpEvent, TurnUsage
from kiro_crew.providers.acp import AcpProvider


def _build_provider(backend: str) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


async def _drain(it):
    out = []
    async for x in it:
        out.append(x)
    return out


def _async_iter(items):
    async def _gen():
        for it in items:
            yield it

    return _gen()


class TestStreamCommandRouting:
    @pytest.mark.asyncio
    async def test_kiro_backend_uses_commands_execute(self):
        provider = _build_provider(backend="")
        provider._client.stream_command = MagicMock(
            return_value=_async_iter([AcpEvent(kind="text_chunk", text="ok")])
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        events = await _drain(provider.stream_command("/compact"))

        provider._client.stream_command.assert_called_once_with("/compact")
        provider._client.stream_events.assert_not_called()
        assert len(events) == 1
        assert events[0].text == "ok"

    @pytest.mark.asyncio
    async def test_claude_backend_uses_session_prompt(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client.stream_events = MagicMock(
            return_value=_async_iter([AcpEvent(kind="text_chunk", text="ok")])
        )
        provider._client.stream_command = MagicMock(return_value=_async_iter([]))

        events = await _drain(provider.stream_command("/compact"))

        provider._client.stream_events.assert_called_once_with("/compact")
        provider._client.stream_command.assert_not_called()
        assert len(events) == 1
        assert events[0].text == "ok"


class TestToLlmEventFieldPropagation:
    """The provider reconstructs each AcpEvent via _to_llm_event; new fields
    used by the Activity tab (tool_final, subagents, sub_session_id) must be
    carried through or downstream consumers see defaults."""

    @pytest.mark.asyncio
    async def test_stream_propagates_diff_old_text_with_content(self):
        """diff_old_text with real content (edit case) survives _to_llm_event."""
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        src = AcpEvent(
            kind="tool_result",
            tool_call_id="tc-diff-1",
            diff_old_text="original content",
            diff_path="/tmp/foo.py",
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([src]))

        events = await _drain(provider.stream("hi"))

        assert len(events) == 1
        ev = events[0]
        assert ev.diff_old_text == "original content"
        assert ev.diff_path == "/tmp/foo.py"

    @pytest.mark.asyncio
    async def test_stream_propagates_diff_old_text_empty_string_create_case(self):
        """diff_old_text="" means file-create (no previous content); must not
        be confused with None (no diff block present / fallback to disk)."""
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        src = AcpEvent(
            kind="tool_result",
            tool_call_id="tc-diff-2",
            diff_old_text="",
            diff_path="/tmp/new_file.py",
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([src]))

        events = await _drain(provider.stream("hi"))

        assert len(events) == 1
        ev = events[0]
        assert ev.diff_old_text == ""
        assert ev.diff_old_text is not None  # explicitly not None
        assert ev.diff_path == "/tmp/new_file.py"

    @pytest.mark.asyncio
    async def test_stream_propagates_diff_old_text_none_no_block_case(self):
        """diff_old_text=None means no diff block was present — provider must
        preserve None so chat_runner falls back to disk read."""
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        src = AcpEvent(
            kind="tool_result",
            tool_call_id="tc-diff-3",
            diff_old_text=None,
            diff_path="",
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([src]))

        events = await _drain(provider.stream("hi"))

        assert len(events) == 1
        ev = events[0]
        assert ev.diff_old_text is None
        assert ev.diff_path == ""

    @pytest.mark.asyncio
    async def test_stream_propagates_tool_final_and_subagent_fields(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        src = AcpEvent(
            kind="tool_result",
            tool_call_id="tc-1",
            tool_output="done",
            tool_final=True,
            sub_session_id="sess-1",
            subagents=[{"sessionId": "sess-1"}],
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([src]))

        events = await _drain(provider.stream("hi"))

        assert len(events) == 1
        ev = events[0]
        assert ev.tool_final is True
        assert ev.tool_output == "done"
        assert ev.sub_session_id == "sess-1"
        assert ev.subagents == [{"sessionId": "sess-1"}]

    @pytest.mark.asyncio
    async def test_stream_propagates_turn_usage(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        src = AcpEvent(
            kind="complete",
            stop_reason="end_turn",
            usage=TurnUsage(
                input_tokens=11,
                output_tokens=22,
                cache_creation_tokens=33,
                cache_read_tokens=44,
            ),
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([src]))

        events = await _drain(provider.stream("hi"))

        assert len(events) == 1
        ev = events[0]
        assert ev.usage.input_tokens == 11
        assert ev.usage.output_tokens == 22
        assert ev.usage.cache_creation_tokens == 33
        assert ev.usage.cache_read_tokens == 44


class TestToLlmEventFieldParity:
    """Structural guard: every field on AcpEvent must either be explicitly
    forwarded in _to_llm_event or listed in a documented allowlist of
    intentionally-dropped fields. This prevents the bug class where a new
    AcpEvent field is silently lost by the provider copy."""

    # Fields that are intentionally NOT forwarded through _to_llm_event.
    # Each entry must document why it is excluded.
    _INTENTIONALLY_DROPPED: set[str] = {
        # ``todo`` is consumed directly by the dashboard websocket handler
        # (EVENT_TODO_UPDATE) and never needs to survive the LLMProvider
        # stream interface — chat_runner does not inspect it.
        "todo",
    }

    def test_all_acp_event_fields_forwarded_or_allowlisted(self):
        """_to_llm_event must forward every AcpEvent field not in the
        intentionally-dropped allowlist."""
        all_fields = {f.name for f in dataclasses.fields(AcpEvent)}
        # Build a source event with kind (required positional)
        src = AcpEvent(kind="test")
        result = AcpProvider._to_llm_event(src)

        forwarded: set[str] = set()
        for f in dataclasses.fields(AcpEvent):
            src_val = getattr(src, f.name)
            out_val = getattr(result, f.name)
            # If the output matches the source default, it was forwarded
            # (since we only set kind, all others are at their defaults).
            if out_val == src_val:
                forwarded.add(f.name)

        # Verify with non-default values to be certain (kind is always set).
        missing = all_fields - forwarded - self._INTENTIONALLY_DROPPED
        assert not missing, (
            f"AcpEvent fields not forwarded by _to_llm_event and not in "
            f"allowlist: {sorted(missing)}. Either add them to _to_llm_event "
            f"or document why in _INTENTIONALLY_DROPPED."
        )

    def test_intentionally_dropped_fields_exist_on_acp_event(self):
        """Guard against stale entries in the allowlist — every listed field
        must actually exist on AcpEvent."""
        all_fields = {f.name for f in dataclasses.fields(AcpEvent)}
        stale = self._INTENTIONALLY_DROPPED - all_fields
        assert not stale, (
            f"Fields in _INTENTIONALLY_DROPPED that no longer exist on "
            f"AcpEvent: {sorted(stale)}. Remove them from the allowlist."
        )


class TestCompactRouting:
    @pytest.mark.asyncio
    async def test_kiro_backend_uses_session_prompt(self):
        """kiro-cli 2.14.0 exits rc=0 on the string form of
        _kiro.dev/commands/execute (live-probe confirmed), so /compact must
        flow through session/prompt on the kiro backend too."""
        provider = _build_provider(backend="")
        provider._client.send_command = AsyncMock(return_value="")
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        await provider.compact()

        provider._client.stream_events.assert_called_once_with("/compact")
        provider._client.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_kiro_backend_prompt_with_context(self):
        provider = _build_provider(backend="")
        provider._client.send_command = AsyncMock(return_value="")
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        await provider.compact("important context")

        provider._client.stream_events.assert_called_once()
        sent = provider._client.stream_events.call_args.args[0]
        assert sent.startswith("/compact ")
        assert "important context" in sent
        provider._client.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_midturn_terminal_status_cached_for_wait(self):
        """kiro-cli may emit the terminal compaction status BEFORE end_turn.
        compact()'s drain must capture it so a subsequent
        wait_for_compaction() (task_executor, wecom, cli_chat) returns it
        instead of stalling 120s and resetting a compacted session."""
        provider = _build_provider(backend="")
        status = AcpEvent(kind="compaction_status", text="completed", title="sum")
        provider._client.stream_events = MagicMock(return_value=_async_iter([status]))
        provider._client._drain_post_compaction_metadata = AsyncMock()
        provider._client.wait_for_compaction = AsyncMock(
            return_value={"type": "timeout"}  # would be WRONG if consulted
        )

        await provider.compact()
        result = await provider.wait_for_compaction(timeout=1.0)

        assert result == {"type": "completed", "summary": "sum"}
        provider._client.wait_for_compaction.assert_not_awaited()
        # The cached completed path must still grace-drain the inner client
        # for kiro's post-compaction metadata, so the mid-turn path reports
        # real numbers too (mirrors AcpSessionHandle.wait_for_compaction).
        provider._client._drain_post_compaction_metadata.assert_awaited_once()
        # Cache is one-shot: the next wait falls through to the client.
        result2 = await provider.wait_for_compaction(timeout=1.0)
        assert result2 == {"type": "timeout"}

    @pytest.mark.asyncio
    async def test_async_status_falls_through_to_client_wait(self):
        """No terminal status mid-turn — wait_for_compaction delegates to the
        client's queue wait (the async-after-end_turn case)."""
        provider = _build_provider(backend="")
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))
        provider._client.wait_for_compaction = AsyncMock(
            return_value={"type": "completed", "summary": ""}
        )

        await provider.compact()
        result = await provider.wait_for_compaction(timeout=1.0)

        assert result["type"] == "completed"
        provider._client.wait_for_compaction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_compact_clears_stale_cached_result(self):
        """A fresh compact() must not leave a PREVIOUS attempt's cached
        result satisfying a later wait."""
        provider = _build_provider(backend="")
        status = AcpEvent(kind="compaction_status", text="completed", title="old")
        provider._client.stream_events = MagicMock(return_value=_async_iter([status]))
        await provider.compact()  # caches "old"

        provider._client.stream_events = MagicMock(return_value=_async_iter([]))
        provider._client.wait_for_compaction = AsyncMock(return_value={"type": "failed"})
        await provider.compact()  # no mid-turn status this time
        result = await provider.wait_for_compaction(timeout=1.0)

        assert result == {"type": "failed"}

    @pytest.mark.asyncio
    async def test_claude_backend_uses_session_prompt(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client.send_command = AsyncMock(return_value="")
        provider._client.stream_events = MagicMock(
            return_value=_async_iter([AcpEvent(kind="text_chunk", text="x")])
        )

        await provider.compact()

        provider._client.stream_events.assert_called_once_with("/compact")
        provider._client.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_claude_backend_truncates_long_context(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        await provider.compact("a" * 5000)

        sent = provider._client.stream_events.call_args.args[0]
        assert sent.startswith("/compact ")
        # context portion should be capped at 4000 chars
        body = sent.split("\n", 1)[1]
        assert len(body) == 4000


class TestEffortControl:
    """Provider-level effort orchestration: backend branch selection,
    capability gating, live-apply, and clear semantics."""

    def _effort_provider(self, backend: str, model: str) -> AcpProvider:
        provider = _build_provider(backend=backend)
        provider._client._model = model
        provider._client._work_dir = MagicMock()
        provider._client.send_command = AsyncMock()
        provider._client.set_config_option = AsyncMock()
        # Default: the session advertises an 'effort' option (modern adapter).
        provider._client.supports_config_option = MagicMock(return_value=True)
        return provider

    @pytest.mark.asyncio
    async def test_kiro_change_effort_pushes_slash_command_and_overlay(self):
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        with patch("kiro_crew.providers.acp._write_cli_overlay") as wco:
            ok = await provider.change_effort("xhigh")
        assert ok is True
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "xhigh"})
        # kiro uses the overlay, never set_config_option
        provider._client.set_config_option.assert_not_awaited()
        wco.assert_called_once()
        assert provider._effort_per_model["claude-opus-4.7"] == "xhigh"

    @pytest.mark.asyncio
    async def test_claude_change_effort_uses_set_config_option(self):
        provider = self._effort_provider(
            backend=ACP_BACKEND_CLAUDE, model="global.anthropic.claude-opus-4-8[1m]"
        )
        ok = await provider.change_effort("high")
        assert ok is True
        provider._client.set_config_option.assert_awaited_once_with("effort", "high")
        # claude does NOT use the kiro /effort slash command
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_change_effort_steps_down_when_max_unsupported(self):
        # Adapter rejects "max" for a model whose ceiling is "xhigh"; the push
        # must fall back down the ladder and land "xhigh" rather than failing
        # the whole change (which would reset the session and lose state).
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")

        async def _reject_max(config_id, value):
            if value == "max":
                raise AcpError("Invalid value for config option effort: max")

        provider._client.set_config_option = AsyncMock(side_effect=_reject_max)
        ok = await provider.change_effort("max")
        assert ok is True
        calls = [c.args for c in provider._client.set_config_option.await_args_list]
        assert ("effort", "max") in calls  # tried the requested level first
        assert ("effort", "xhigh") in calls  # then stepped down and succeeded
        # The slot override keeps the requested level so a future
        # max-capable model would get it.
        assert provider._effort_per_model["claude-opus-4.7"] == "max"

    @pytest.mark.asyncio
    async def test_claude_change_effort_propagates_non_value_errors(self):
        # A transport/timeout error is NOT a value rejection — it must NOT be
        # swallowed by the ladder; it propagates so the caller rolls back.
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.set_config_option = AsyncMock(side_effect=AcpError("transport died"))
        with pytest.raises(AcpError, match="transport died"):
            await provider.change_effort("high")

    @pytest.mark.asyncio
    async def test_change_effort_noop_on_incapable_model(self):
        # 'auto' is genuinely effort-incapable (no concrete model selected).
        provider = self._effort_provider(backend="", model="auto")
        ok = await provider.change_effort("high")
        assert ok is False
        provider._client.send_command.assert_not_awaited()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_rejects_invalid_level(self):
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        with pytest.raises(ValueError):
            await provider.change_effort("ultra")

    @pytest.mark.asyncio
    async def test_claude_apply_initial_effort_pushes_resolved_level(self):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_awaited_once_with("effort", "max")

    @pytest.mark.asyncio
    async def test_apply_initial_effort_noop_on_kiro_backend(self):
        # kiro gets effort from the spawn-time overlay, not a live push.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_apply_initial_effort_swallows_adapter_error(self):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("bad"))
        # Must not raise — a rejected effort cannot break session start.
        await provider._apply_initial_effort()

    @pytest.mark.asyncio
    async def test_claude_initial_effort_skips_when_option_unsupported(self):
        # Older claude-agent-acp builds advertise no 'effort' config option;
        # the initial-effort push must be a silent no-op (no set_config_option,
        # no error) rather than spamming 'Unknown config option' every spawn.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        provider._client.supports_config_option = MagicMock(return_value=False)
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_change_effort_returns_false_when_option_unsupported(self):
        # change_effort must report unsupported (False) instead of attempting a
        # push that fails with 'Unknown config option' and resets the session.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.supports_config_option = MagicMock(return_value=False)
        ok = await provider.change_effort("high")
        assert ok is False
        provider._client.set_config_option.assert_not_awaited()
        # No poisoned override left behind.
        assert "claude-opus-4.7" not in provider._effort_per_model

    @pytest.mark.asyncio
    async def test_claude_set_effort_swallows_unknown_config_option(self):
        # Defense in depth: even if the capability guard is bypassed (e.g. the
        # option is advertised lazily), an 'Unknown config option' rejection
        # from the adapter must be skipped, not re-raised (which resets).
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        # Force the guard open so the ladder runs and hits the adapter error.
        provider._client.supports_config_option = MagicMock(return_value=True)
        provider._client.set_config_option = AsyncMock(
            side_effect=AcpError(
                "JSON-RPC error: {'code': -32603, 'message': 'Internal error', "
                "'data': {'details': 'Unknown config option: effort'}}"
            )
        )
        # Must not raise.
        await provider._set_claude_effort("max")

    @pytest.mark.asyncio
    async def test_kiro_clear_effort_no_default_returns_false_for_reset(self):
        # No workspace default resolves → overlay cleared, nothing pushed live,
        # and clear_effort returns FALSE so the handler resets the session
        # (kiro respawns at the model's built-in default). Returning True here
        # would leave the running session stuck at the old effort.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "high"}
        with patch("kiro_crew.providers.acp._clear_cli_overlay_effort") as cco:
            ok = await provider.clear_effort()
        assert ok is False
        assert "claude-opus-4.7" not in provider._effort_per_model
        cco.assert_called_once()
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_clear_effort_returns_false_for_reset(self):
        # claude-agent-acp has no "reset to default" config value, so clearing
        # must return False to trigger a session reset; it must NOT push.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        ok = await provider.clear_effort()
        assert ok is False
        assert "claude-opus-4.7" not in provider._effort_per_model
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_rolls_back_on_push_failure(self):
        # A failed live push must not leave a poisoned override/overlay that
        # would re-push the rejected level on every respawn.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("rejected"))
        with pytest.raises(RuntimeError):
            await provider.change_effort("xhigh")
        # Override rolled back (was previously unset).
        assert "claude-opus-4.7" not in provider._effort_per_model

    @pytest.mark.asyncio
    async def test_kiro_change_effort_gpt_uses_reasoning_overlay_key(self):
        # GPT models are effort-capable on the kiro backend; the live push is the
        # same /effort slash command, but the spawn overlay must use the GPT
        # `reasoning` key (verified against kiro 2.13) — not `output_config`.
        provider = self._effort_provider(backend="", model="gpt-5.6-luna")
        with patch("kiro_crew.providers.acp._write_cli_overlay") as wco:
            ok = await provider.change_effort("max")
        assert ok is True
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "max"})
        wco.assert_called_once()
        assert provider._effort_per_model["gpt-5.6-luna"] == "max"

    def test_supports_effort_reflects_model(self):
        assert self._effort_provider(backend="", model="claude-opus-4.7").supports_effort()
        # GPT models are effort-capable on the kiro backend.
        assert self._effort_provider(backend="", model="gpt-5.6-luna").supports_effort()
        # 'auto' is genuinely effort-incapable (no concrete model selected). A raw
        # kiro 'claude-haiku-4.5' is also incapable — the haiku guard wins over
        # the registry's Sonnet fold (see test_effort.py). deepseek is a
        # third-party model kiro does not offer effort on.
        assert not self._effort_provider(backend="", model="auto").supports_effort()
        assert not self._effort_provider(backend="", model="claude-haiku-4.5").supports_effort()
        assert not self._effort_provider(backend="", model="deepseek-3.2").supports_effort()


class TestStartKiroRuntimeResume:
    """_start_kiro_runtime resume path: resume issues runtime.load_session()
    DIRECTLY (session/load, no session/new first) with the full transcript PATH
    (~/.kiro/sessions/cli/<sid>.json), guarded on the transcript existing on
    disk. A missing / empty / failed resume falls back to create_session().
    The direct-load flow mirrors AcpClient and avoids the double-context
    'refusal' failure mode."""

    def _kiro_provider(self, model="auto"):
        provider = _build_provider(backend="")  # kiro backend
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_settings_mcp_json = None
        provider._client._mcp_gateway_socket = None
        # _model is a real string (not a MagicMock) so the DEFAULT_MODEL guard
        # in _start_kiro_runtime compares correctly.
        provider._client._model = model
        return provider

    async def _run_start(self, provider, resume_sid, file_exists, load_raises=False):
        mock_handle = MagicMock()
        mock_handle.session_id = "kiro-sess-1"
        mock_handle.store_session_config = MagicMock()
        mock_handle.set_model = AsyncMock()
        mock_runtime = MagicMock()
        mock_runtime.pid = 4321
        mock_runtime.spawn = AsyncMock()
        mock_runtime.create_session = AsyncMock(return_value=mock_handle)
        if load_raises:
            mock_runtime.load_session = AsyncMock(side_effect=RuntimeError("load boom"))
        else:
            mock_runtime.load_session = AsyncMock(return_value=mock_handle)
        provider._client._resume_session_id = resume_sid

        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=mock_runtime),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda handle, runtime, **kw: MagicMock(
                    _handle=handle, _runtime=runtime, resumed=False
                ),
            ),
            patch("pathlib.Path.exists", return_value=file_exists),
        ):
            await provider._start_kiro_runtime()
        return mock_handle, mock_runtime

    @pytest.mark.asyncio
    async def test_resume_loads_full_transcript_path(self):
        provider = self._kiro_provider()
        _handle, runtime = await self._run_start(provider, "abc-123", file_exists=True)
        runtime.load_session.assert_awaited_once()
        # session/load must be issued directly — no session/new first.
        runtime.create_session.assert_not_awaited()
        args = runtime.load_session.await_args.args
        loaded_path, loaded_sid = args[0], args[1]
        # First positional is the full transcript path, never the bare sid.
        assert loaded_path.endswith("/.kiro/sessions/cli/abc-123.json")
        assert loaded_path != "abc-123"
        # Second positional is the original sid, adopted as the resumed sessionId.
        assert loaded_sid == "abc-123"

    @pytest.mark.asyncio
    async def test_resume_skipped_when_transcript_missing(self):
        provider = self._kiro_provider()
        _handle, runtime = await self._run_start(provider, "stale-sid", file_exists=False)
        # A stale sid with no transcript on disk must NOT replay — fresh start.
        runtime.load_session.assert_not_awaited()
        runtime.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_resume_when_sid_empty(self):
        provider = self._kiro_provider()
        _handle, runtime = await self._run_start(provider, "", file_exists=True)
        runtime.load_session.assert_not_awaited()
        runtime.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_load_falls_back_to_create_session(self):
        # session/load raising (e.g. capability guard, missing "modes") must not
        # abort startup — it falls back to a fresh session/new.
        provider = self._kiro_provider()
        _handle, runtime = await self._run_start(
            provider, "abc-123", file_exists=True, load_raises=True
        )
        runtime.load_session.assert_awaited_once()
        runtime.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_default_model_applied_to_runtime_session(self):
        # A slot configured with a non-default kiro model must get set_model on
        # the live session (mirrors AcpClient handshake step 5) — else it
        # silently runs the agent's default model.
        provider = self._kiro_provider(model="global.anthropic.claude-opus-4-8[1m]")
        handle, _runtime = await self._run_start(provider, "", file_exists=True)
        handle.set_model.assert_awaited_once_with("global.anthropic.claude-opus-4-8[1m]")

    @pytest.mark.asyncio
    async def test_default_model_does_not_call_set_model(self):
        # DEFAULT_MODEL ("auto") = let kiro-cli pick per agent config → no push.
        provider = self._kiro_provider(model="auto")
        handle, _runtime = await self._run_start(provider, "", file_exists=True)
        handle.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_session_failure_kills_orphaned_runtime(self):
        # Resource-leak guard: if create_session() raises AFTER a successful
        # spawn(), nothing owns the kiro-cli process yet — _start_kiro_runtime
        # must kill the runtime before propagating so it isn't orphaned until
        # gateway restart. The original exception must still surface.
        provider = self._kiro_provider(model="auto")
        provider._client._resume_session_id = ""  # no resume → straight to create_session

        mock_runtime = MagicMock()
        mock_runtime.pid = 4321
        mock_runtime.spawn = AsyncMock()
        mock_runtime.kill = AsyncMock()
        mock_runtime.saw_not_logged_in = MagicMock(return_value=False)
        boom = RuntimeError("session limit reached")
        mock_runtime.create_session = AsyncMock(side_effect=boom)

        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=mock_runtime),
            patch("kiro_crew.providers.acp.AcpSessionProvider"),
        ):
            with pytest.raises(RuntimeError, match="session limit reached"):
                await provider._start_kiro_runtime()

        # The orphaned runtime was killed exactly once before the raise propagated.
        mock_runtime.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_successful_start_does_not_kill_runtime(self):
        # Guard against over-eager cleanup: a normal successful start must NOT
        # kill the runtime (the AcpSessionProvider now owns it).
        provider = self._kiro_provider(model="auto")
        _handle, runtime = await self._run_start(provider, "", file_exists=True)
        runtime.kill.assert_not_called()


class _CapturingRecorder:
    """Records every histogram() call's (name, attrs) — a metrics-recorder
    stand-in so the startup-metric tests never touch a real exporter."""

    def __init__(self) -> None:
        self.calls: list = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.calls.append((name, dict(attrs or {})))


class TestKiroStartupMetric:
    """The kiro cold-start (the DEFAULT backend) must emit
    ``kirocrew.session.startup.duration`` tagged ``backend=kiro`` with a phase
    split (total + spawn_init + session_new [+ set_model]) so the dominant
    session/new MCP-toolset load is measurable. AcpClient.ensure_ready already
    covers the claude path; this covers the kiro path."""

    def _kiro_provider(self, model="auto"):
        provider = _build_provider(backend="")  # kiro backend
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_settings_mcp_json = None
        provider._client._mcp_gateway_socket = None
        provider._client._resume_session_id = ""
        provider._client._model = model
        return provider

    async def _start(self, provider, *, spawn_exc=None):
        mock_handle = MagicMock()
        mock_handle.session_id = "kiro-sess-1"
        mock_handle.set_model = AsyncMock()
        mock_runtime = MagicMock()
        mock_runtime.pid = 4321
        mock_runtime.spawn = AsyncMock(side_effect=spawn_exc)
        mock_runtime.saw_not_logged_in = MagicMock(return_value=bool(spawn_exc))
        mock_runtime.kill = AsyncMock()
        mock_runtime.create_session = AsyncMock(return_value=mock_handle)
        rec = _CapturingRecorder()
        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=mock_runtime),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda handle, runtime, **kw: MagicMock(resumed=False),
            ),
            patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        ):
            await provider._start_kiro_runtime()
        return rec

    def _phases(self, rec):
        assert rec.calls, "kiro startup histogram must be emitted"
        for name, _ in rec.calls:
            assert name == "kirocrew.session.startup.duration"
        return {a["phase"]: a for _, a in rec.calls}

    @pytest.mark.asyncio
    async def test_emits_total_and_phase_split_on_success(self):
        provider = self._kiro_provider(model="auto")
        rec = await self._start(provider)
        phases = self._phases(rec)
        # total + the two mandatory phases (auto model → no set_model phase).
        assert {"total", "spawn_init", "session_new"} <= set(phases)
        for attrs in phases.values():
            assert attrs["backend"] == "kiro"
            assert attrs["outcome"] == "ready"

    @pytest.mark.asyncio
    async def test_set_model_phase_only_for_non_default_model(self):
        provider = self._kiro_provider(model="claude-sonnet-5")
        rec = await self._start(provider)
        phases = self._phases(rec)
        assert "set_model" in phases  # a non-"auto" model adds the phase

    @pytest.mark.asyncio
    async def test_auth_required_outcome(self):
        from kiro_crew.acp.runtime import AcpRuntimeError

        provider = self._kiro_provider()
        mock_runtime = MagicMock()
        mock_runtime.spawn = AsyncMock(side_effect=AcpRuntimeError("boom"))
        mock_runtime.saw_not_logged_in = MagicMock(return_value=True)
        mock_runtime.kill = AsyncMock()
        rec = _CapturingRecorder()
        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=mock_runtime),
            patch("kiro_crew.metrics.provider.get_recorder", return_value=rec),
        ):
            with pytest.raises(AcpAuthRequired):
                await provider._start_kiro_runtime()
        phases = self._phases(rec)
        assert phases["total"]["outcome"] == "auth_required"


# ── Fix B: dead runtime in fresh-start fallback ──────────────────────────────


class TestFixBDeadRuntimeRespawn:
    """Fix B: if runtime dies during resume, the fresh-start fallback respawns
    it transparently instead of raising AcpRuntimeDead."""

    def _kiro_provider(self):
        provider = _build_provider(backend="")  # kiro backend
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_settings_mcp_json = None
        provider._client._mcp_gateway_socket = None
        provider._client._model = "auto"
        return provider

    @pytest.mark.asyncio
    async def test_dead_runtime_respawned_and_create_session_on_new(self):
        """When runtime.is_alive() returns False after failed resume,
        a new runtime is spawned and create_session is called on it."""
        provider = self._kiro_provider()

        # First runtime: spawns OK but dies during resume
        dead_runtime = MagicMock()
        dead_runtime.pid = 1111
        dead_runtime.spawn = AsyncMock()
        dead_runtime.is_alive = MagicMock(return_value=False)  # dead!
        dead_runtime.kill = AsyncMock()
        dead_runtime.load_session = AsyncMock(side_effect=RuntimeError("load failed"))

        # Second runtime: the respawned one
        new_handle = MagicMock()
        new_handle.session_id = "fresh-sess"
        new_handle.set_model = AsyncMock()
        new_handle.store_session_config = MagicMock()
        new_runtime = MagicMock()
        new_runtime.pid = 2222
        new_runtime.spawn = AsyncMock()
        new_runtime.is_alive = MagicMock(return_value=True)
        new_runtime.create_session = AsyncMock(return_value=new_handle)
        new_runtime.saw_not_logged_in = MagicMock(return_value=False)

        provider._client._resume_session_id = "old-sess-id"

        # AcpRuntime() called twice: first returns dead_runtime, second returns new_runtime
        runtime_calls = iter([dead_runtime, new_runtime])

        with (
            patch(
                "kiro_crew.providers.acp.AcpRuntime",
                side_effect=lambda **kw: next(runtime_calls),
            ),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda handle, runtime, **kw: MagicMock(
                    _handle=handle, _runtime=runtime, resumed=False
                ),
            ),
            patch("pathlib.Path.exists", return_value=True),
        ):
            await provider._start_kiro_runtime()

        # The dead runtime was killed
        dead_runtime.kill.assert_awaited_once()
        # The new runtime was spawned
        new_runtime.spawn.assert_awaited_once()
        # create_session called on the NEW runtime (not the dead one)
        new_runtime.create_session.assert_awaited_once()
        dead_runtime.create_session.assert_not_called()


class TestLoadSessionWithRetry:
    """F2 load-recovery Phase 1: ``_load_session_with_retry`` retries past a
    stale 'active in another process' lock, and returns None (caller falls back
    to a fresh session + history replay) on a persistent lock, a non-lock load
    error, or a dead runtime."""

    @staticmethod
    def _runtime(load_session, *, alive: bool = True) -> MagicMock:
        rt = MagicMock()
        rt.is_alive.return_value = alive
        rt.load_session = load_session
        return rt

    @pytest.mark.asyncio
    async def test_fast_path_success_no_retry(self):
        provider = _build_provider(backend="")
        handle = object()
        rt = self._runtime(AsyncMock(return_value=handle))
        with patch("kiro_crew.providers.acp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            got = await provider._load_session_with_retry(rt, "/s.json", "sid", None, None)
        assert got is handle
        assert rt.load_session.await_count == 1
        assert sleep_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_recovers_after_stale_lock_releases(self):
        provider = _build_provider(backend="")
        handle = object()
        rt = self._runtime(
            AsyncMock(
                side_effect=[
                    RuntimeError("kiro session sid is active in another process"),
                    RuntimeError("kiro session sid is active in another process"),
                    handle,
                ]
            )
        )
        with patch("kiro_crew.providers.acp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            got = await provider._load_session_with_retry(rt, "/s.json", "sid", None, None)
        assert got is handle
        assert rt.load_session.await_count == 3
        assert sleep_mock.await_count == 2  # backoff before attempts 2 and 3

    @pytest.mark.asyncio
    async def test_persistent_lock_exhausts_and_falls_back(self):
        from kiro_crew.providers.acp import _RESUME_MAX_ATTEMPTS

        provider = _build_provider(backend="")
        rt = self._runtime(
            AsyncMock(side_effect=RuntimeError("session is active in another process"))
        )
        with patch("kiro_crew.providers.acp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            got = await provider._load_session_with_retry(rt, "/s.json", "sid", None, None)
        assert got is None
        assert rt.load_session.await_count == _RESUME_MAX_ATTEMPTS
        assert sleep_mock.await_count == _RESUME_MAX_ATTEMPTS - 1

    @pytest.mark.asyncio
    async def test_non_lock_error_does_not_retry(self):
        provider = _build_provider(backend="")
        rt = self._runtime(AsyncMock(side_effect=RuntimeError("session/load parse error")))
        with patch("kiro_crew.providers.acp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            got = await provider._load_session_with_retry(rt, "/s.json", "sid", None, None)
        assert got is None
        assert rt.load_session.await_count == 1  # a genuine failure is not retried
        assert sleep_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_dead_runtime_stops_retry(self):
        provider = _build_provider(backend="")
        rt = self._runtime(
            AsyncMock(side_effect=RuntimeError("active in another process")),
            alive=False,
        )
        with patch("kiro_crew.providers.acp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            got = await provider._load_session_with_retry(rt, "/s.json", "sid", None, None)
        assert got is None
        assert rt.load_session.await_count == 1  # bail as soon as the runtime is dead
        assert sleep_mock.await_count == 0


class TestStartKiroRuntimeModelEntitlement:
    """_start_kiro_runtime withholds a configured model the account cannot run.

    This is the path real dashboard sessions take. Sending an unusable model
    here is what produced a failed turn every time: kiro-cli ACCEPTS the id at
    session/set_model, so nothing fails locally, and only the service rejects
    it mid-prompt as "-32603 ... model is not available".
    """

    def _kiro_provider(self, model):
        provider = _build_provider(backend="")  # kiro backend
        provider._client._work_dir = "/tmp/ws"
        provider._client._agent = "kirocrew"
        provider._client._sandbox_mode = "auto"
        provider._client._extra_env = {}
        provider._client._mcp_gateway_overlay = None
        provider._client._mcp_gateway_settings_mcp_json = None
        provider._client._mcp_gateway_socket = None
        provider._client._model = model
        provider._client._resume_session_id = ""  # straight to create_session
        return provider

    async def _run(self, model, advertised):
        provider = self._kiro_provider(model)
        handle = MagicMock()
        handle.session_id = "kiro-sess-1"
        handle.store_session_config = MagicMock()
        handle.set_model = AsyncMock()
        handle.available_models = [{"modelId": m, "name": m} for m in advertised]
        runtime = MagicMock()
        runtime.pid = 4321
        runtime.spawn = AsyncMock()
        runtime.create_session = AsyncMock(return_value=handle)

        with (
            patch("kiro_crew.providers.acp.AcpRuntime", return_value=runtime),
            patch(
                "kiro_crew.providers.acp.AcpSessionProvider",
                side_effect=lambda h, r, **kw: MagicMock(_handle=h, _runtime=r, resumed=False),
            ),
            patch("pathlib.Path.exists", return_value=False),
        ):
            await provider._start_kiro_runtime()
        return handle

    @pytest.mark.asyncio
    async def test_unusable_configured_model_is_never_sent(self):
        handle = await self._run("claude-opus-4.8", ["claude-sonnet-4.6"])
        handle.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_usable_configured_model_is_applied(self):
        handle = await self._run("claude-opus-4.8", ["claude-sonnet-4.6", "claude-opus-4.8"])
        handle.set_model.assert_awaited_once_with("claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_unknown_entitlement_still_applies(self):
        """No advertised list means unknowable, so behaviour is unchanged."""
        handle = await self._run("claude-opus-4.8", [])
        handle.set_model.assert_awaited_once_with("claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_auto_sentinel_never_reaches_the_check(self):
        handle = await self._run("auto", ["claude-sonnet-4.6"])
        handle.set_model.assert_not_awaited()
