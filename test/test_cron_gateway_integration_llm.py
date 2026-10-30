"""Integration tests for LLM-message cron turns in the gateway.

Admission of an LLM cron turn, the per-job model override and its fallback, the
throttle fallback wiring, and the usage row a model-spending cron turn appends.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_gateway_integration_helpers import (
    _make_command_job,
    _make_gw,
    _make_gw_for_llm,
    _make_llm_job,
    _make_script_job,
    _run_command_callback,
    _run_llm_callback,
    _run_script_callback,
)

from kiro_crew.testing.wait import finished

# ── Per-job model override: _acquire_with_model_fallback / _annotate_model_downgrade ──


class TestLlmCronAdmission:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_modes", [False, True])
    async def test_execution_binding_runs_off_loop(self, monkeypatch, with_modes):
        from kiro_crew import execution_context

        gw = _make_gw_for_llm()
        gw.ctx_builder._session_memory_modes = {} if with_modes else None
        job = _make_llm_job()
        loop_thread = threading.get_ident()
        bindings = []
        original = execution_context.bind_session_execution

        def bind(key, execution):
            bindings.append((threading.get_ident(), key, execution))
            return original(key, execution)

        monkeypatch.setattr(execution_context, "bind_session_execution", bind)
        result, _ = await finished(_run_llm_callback(gw, job), "the LLM callback", timeout=10)
        assert result == "Agent response here"
        # Mode publication also tightens the same canonical record.
        assert len(bindings) == (2 if with_modes else 1)
        thread, key, captured = bindings[0]
        assert all(thread != loop_thread for thread, _, _ in bindings)
        assert key == f"cron:{job.id}"
        assert captured == execution_context.read_session_execution(key, required=True)

    @pytest.mark.asyncio
    async def test_session_closing_retains_undispatched_one_shot(self):
        from kiro_crew.session import SessionClosingError

        gw = _make_gw_for_llm()
        job = _make_llm_job(
            delete_after_run=True,
            last_result="stale result",
            consecutive_failures=2,
        )

        async def _side_effect(*_args, **_kwargs):
            raise SessionClosingError("automatic update owns admission")

        result, stream = await _run_llm_callback(
            gw,
            job,
            get_or_create_side_effect=_side_effect,
        )

        assert result is None
        assert job.run_never_started is True
        assert job.last_status == "error"
        assert job.last_error == "gateway admission is closed"
        assert job.last_result == ""
        assert job.consecutive_failures == 2
        stream.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy_agent", ["alice", "named-template"])
    async def test_an_upgraded_member_schedule_naming_an_agent_fires(
        self, monkeypatch, tmp_path, legacy_agent
    ):
        """The upgrade's capture and the old build's session record agree at dispatch.

        0.7.0-insider.1 to .5 fired a member schedule once and left
        ``{memory_store, agent: <member selector>}`` under ``cron:<id>``. The
        start-of-process capture keeps the ``agent_id`` the schedule named, and
        the single-agent fire then binds that capture under the same stable key
        with ``replace_existing`` False -- which refused the record until the
        capture reconciled it. Driven through the real callback: the fire runs,
        dispatches the named agent, and the record carries the capture.
        """
        import json
        import os
        from pathlib import Path
        from types import SimpleNamespace

        from kiro_crew import execution_context
        from kiro_crew.config import loader
        from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig
        from kiro_crew.cron_service.identity import migrate_legacy_member_schedules
        from kiro_crew.cron_service.store import _job_from_record
        from kiro_crew.history import ConversationLog
        from kiro_crew.vector_memory import create_member_database

        home = Path(os.environ["KIROCREW_HOME"])
        db = home / "memory_stores" / "member-alice" / "memory.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        create_member_database(db, member_id="id-alice", store_id="member-alice")
        cfg = SimpleNamespace(
            agents={
                "alice": KiroCrewAgentConfig(
                    member_id="id-alice", memory_store="member-alice", kiro_agent="shared"
                )
            },
            memory_stores={
                "member-alice": MemoryStoreConfig(
                    owner_member="alice", owner_member_id="id-alice", memory_version=2
                )
            },
        )
        loader._invalidate_config_cache()
        monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
        execution_context._LIVE_EXECUTIONS.clear()

        store_dir = tmp_path / "cron-store"
        store_dir.mkdir()
        record = {
            "id": "lj1",
            "name": "llm-job",
            "message": "Run daily check",
            "schedule": {"kind": "every", "every_secs": 3600},
            "agent_id": "named-template",
            "member_id": "alice",
            "memory_store": "member-alice",
        }
        (store_dir / "crons.json").write_text(
            json.dumps({"version": 2, "jobs": [record]}), encoding="utf-8"
        )
        ConversationLog().update_metadata(
            "cron:lj1", {"memory_store": "member-alice", "agent": legacy_agent}
        )

        assert migrate_legacy_member_schedules(store_dir) == ["lj1"]
        saved = json.loads((store_dir / "crons.json").read_text(encoding="utf-8"))["jobs"][0]
        job = _job_from_record(saved)
        assert job.agent_id == "named-template"
        assert job.persistent_session is True

        gw = _make_gw_for_llm()
        result, stream = await _run_llm_callback(gw, job, fires=2)

        assert result == "Agent response here"
        assert job.last_status != "error", job.last_error
        assert job.consecutive_failures == 0
        assert stream.await_count == 2
        dispatched = [c.kwargs.get("agent") for c in gw.sessions.get_or_create.call_args_list]
        assert dispatched == ["named-template", "named-template"]
        recorded = execution_context.read_session_execution("cron:lj1", required=True)
        assert recorded.template_id == "named-template"
        assert recorded.member_id == "id-alice"
        assert recorded.store.store_id == "member-alice"

    @pytest.mark.asyncio
    async def test_a_refused_legacy_member_schedule_is_an_ordinary_failed_run(
        self, monkeypatch, tmp_path
    ):
        """A pre-identity schedule refused before dispatch fails like any other refusal.

        The gateway gives ``LegacyScheduleRefused`` no handling of its own: nothing
        is dispatched, no alert is sent, no runtime marker is set, and the raise
        reaches the scheduler, whose catch records an ordinary failed run whose
        error text is the ``memory_unavailable:`` refusal naming the repair.
        """
        from kiro_crew import cron
        from kiro_crew.cron_service.identity import LegacyScheduleRefused

        def refuse(job, **_kwargs):
            raise LegacyScheduleRefused("its store's member was deleted", "delete this schedule")

        monkeypatch.setattr(cron, "resolve_cron_memory", refuse)
        gw = _make_gw_for_llm()
        job = _make_llm_job(member_id="alice", memory_store="member-alice")
        with pytest.raises(LegacyScheduleRefused, match="delete this schedule"):
            await _run_llm_callback(gw, job)
        gw.sessions.get_or_create.assert_not_awaited()
        gw.dashboard_state.notify.assert_not_called()
        assert job.run_never_started is False
        assert job.fire_time_denied is False

        async def refusing_callback(job):
            raise LegacyScheduleRefused("its store's member was deleted", "delete this schedule")

        svc = cron.CronService(base_dir=tmp_path, on_job=refusing_callback)
        await svc._execute(job)
        assert job.last_status == "error"
        assert job.last_error.startswith("memory_unavailable:")
        assert "delete this schedule" in job.last_error

    @pytest.mark.asyncio
    async def test_other_refusals_before_dispatch_still_raise(self, monkeypatch):
        from kiro_crew import cron

        def refuse(job, **_kwargs):
            raise ValueError("memory_unavailable: schedule has no canonical execution context")

        monkeypatch.setattr(cron, "resolve_cron_memory", refuse)
        gw = _make_gw_for_llm()
        with pytest.raises(ValueError, match="no canonical execution context"):
            await _run_llm_callback(gw, _make_llm_job(member_id="deleted"))
        gw.dashboard_state.notify.assert_not_called()


class TestModelFallback:
    """Test _acquire_with_model_fallback and _annotate_model_downgrade paths."""

    @pytest.mark.asyncio
    async def test_model_override_passed_to_session(self):
        """When job.model is set, get_or_create receives it."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        result, _ = await _run_llm_callback(gw, job)
        # The first get_or_create call should have model=job.model
        call_kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert call_kwargs["model"] == "claude-opus-4-8"
        assert result == "Agent response here"

    @pytest.mark.asyncio
    async def test_no_model_passes_none(self):
        """When job.model is empty, get_or_create receives model=None."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")

        result, _ = await _run_llm_callback(gw, job)
        call_kwargs = gw.sessions.get_or_create.call_args_list[0].kwargs
        assert call_kwargs["model"] is None

    @pytest.mark.asyncio
    async def test_model_unavailable_falls_back(self):
        """When pinned model fails with model-related error, retries without model."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-nonexistent-9")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-nonexistent-9' not found")
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert call_count[0] == 2
        assert "unavailable" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_model_fallback_annotates_response(self):
        """Downgraded result is prefixed with a warning annotation."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-fancy-model")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-fancy-model' is not available")
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert result.startswith("⚠️")
        assert "claude-fancy-model" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_non_model_error_propagates(self):
        """Errors unrelated to model are not caught by the fallback."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        async def _side_effect(*args, **kwargs):
            raise RuntimeError("connection refused to provider host")

        with pytest.raises(RuntimeError, match="connection refused"):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)

    @pytest.mark.asyncio
    async def test_no_fallback_when_model_empty(self):
        """When job.model is empty, any error propagates (no fallback needed)."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")

        async def _side_effect(*args, **kwargs):
            raise RuntimeError("model spawn failed")

        with pytest.raises(RuntimeError, match="model spawn failed"):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)


class TestThrottleFallbackCronWiring:
    """agent.fallback_model reaches the cron turn, and a fallback-served run
    is visibly annotated (never silent)."""

    @pytest.mark.asyncio
    async def test_chain_is_threaded_into_stream_and_collect(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        with patch(
            "kiro_crew.slack.gateway.configured_fallback_chain",
            return_value=("fb-1", "fb-2"),
        ):
            _result, stream_mock = await _run_llm_callback(gw, job)
        assert stream_mock.await_args.kwargs["fallback_models"] == ("fb-1", "fb-2")

    @pytest.mark.asyncio
    async def test_fallback_served_run_is_annotated(self):
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        provider_mock = MagicMock()
        setattr(provider_mock, TURN_FALLBACK_ATTR, ("primary-m", "fb-m"))

        async def _side_effect(*args, **kwargs):
            return (provider_mock, True, False)

        result, _ = await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert result.startswith("⚠️ Model 'primary-m' throttled")
        assert "fb-m" in result
        assert "Agent response here" in result

    @pytest.mark.asyncio
    async def test_fallback_served_run_blanks_pinned_model_in_usage_row(self):
        """A pinned job.model must NOT be billed while a fallback served the
        turn — the usage row blanks it so model_source reports what ran."""
        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="pinned-model")
        provider_mock = MagicMock()
        setattr(provider_mock, TURN_FALLBACK_ATTR, ("pinned-model", "fb-m"))

        async def _side_effect(*args, **kwargs):
            return (provider_mock, True, False)

        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock
        ) as persist_mock:
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)
        assert persist_mock.await_count >= 1
        # Positional arg 1 is the model; blank = defer to model_source.
        assert persist_mock.await_args.args[1] == ""

    @pytest.mark.asyncio
    async def test_unswapped_run_still_bills_pinned_model(self):
        """No fallback marker -> the explicit pin is recorded unchanged."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="pinned-model")
        with patch(
            "kiro_crew.slack.gateway.persist_token_record_async", new_callable=AsyncMock
        ) as persist_mock:
            await _run_llm_callback(gw, job)
        assert persist_mock.await_count >= 1
        assert persist_mock.await_args.args[1] == "pinned-model"

    def test_annotate_noop_without_marker(self):
        from types import SimpleNamespace

        from kiro_crew.slack.gateway import _annotate_model_fallback

        provider = SimpleNamespace()
        assert _annotate_model_fallback("text", provider) == "text"

    def test_annotate_malformed_marker_is_noop(self):
        from types import SimpleNamespace

        from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR
        from kiro_crew.slack.gateway import _annotate_model_fallback

        provider = SimpleNamespace()
        setattr(provider, TURN_FALLBACK_ATTR, ("only-one",))
        assert _annotate_model_fallback("text", provider) == "text"

    def test_gateway_annotator_is_the_shared_body(self):
        """DRIFT PIN: the gateway name must BE the shared
        helper next to TURN_FALLBACK_ATTR — not a re-spelled copy."""
        from kiro_crew.llm_helpers import annotate_model_fallback
        from kiro_crew.slack.gateway import _annotate_model_fallback

        assert _annotate_model_fallback is annotate_model_fallback

    @pytest.mark.asyncio
    async def test_chain_exhaustion_story_reaches_the_failure_alert(self):
        """A cron turn failing after the chain exhausted must
        alert with the WHOLE walk (the story the walk attached to the
        exception), not just the last candidate's error — on BOTH the
        dashboard notify and the Slack DM legs, and even when the backend
        error is verbose enough to hit the detail cap (a real ACP throttle
        payload routinely exceeds it)."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.llm_helpers import FALLBACK_STORY_ATTR

        gw = _make_gw_for_llm()
        job = _make_llm_job(model="")
        # The Slack DM leg: slack client present, no channel delivery.
        gw.slack = MagicMock()
        gw.slack.post_message = AsyncMock()
        gw._open_dm_with_retry = AsyncMock(return_value="D123")
        gw._deliver_cron_to_channel = AsyncMock(return_value=False)

        # Verbose error: longer than _CRON_FAILURE_DETAIL_CAP so the story
        # would be sliced away if it were appended after the cap.
        exc = AcpError("Internal error: API Error: " + "x" * 700)
        setattr(
            exc, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-1, fb-2 also unavailable"
        )

        captured_cb = None
        provider_mock = MagicMock()
        gw.sessions.get_or_create = AsyncMock(return_value=(provider_mock, True, False))
        _embed_mock = AsyncMock(return_value=("full prompt", None))
        _stream_mock = AsyncMock(side_effect=exc)

        with (
            patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls,
            patch("kiro_crew.slack.gateway.run_in_embed_pool", _embed_mock),
            patch("kiro_crew.slack.gateway.stream_and_collect", _stream_mock),
            patch("kiro_crew.slack.gateway.sel"),
            patch("kiro_crew.slack.gateway.build_cron_session_context") as mock_ctx,
        ):
            mock_ctx.return_value = (f"cron:{job.id}", job.message)

            def capture_cron(on_job=None, **kw):
                nonlocal captured_cb
                captured_cb = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                svc.remove_job_async = AsyncMock(return_value=True)
                return svc

            mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
            await gw._init_cron()
            assert captured_cb is not None
            with pytest.raises(AcpError):
                await captured_cb(job)

            # Same backend error, DIFFERENT walk (a candidate momentarily
            # unadvertised changes the story): the dedup hash keys on the
            # story-free error text, so the repeat is suppressed instead of
            # re-paging the user.
            exc2 = AcpError("Internal error: API Error: " + "x" * 700)
            setattr(
                exc2, FALLBACK_STORY_ATTR, "primary-m throttled; fallbacks fb-2 also unavailable"
            )
            _stream_mock.side_effect = exc2
            with pytest.raises(AcpError):
                await captured_cb(job)

        bodies = [
            str(c.args[2]) for c in gw.dashboard_state.notify.call_args_list if len(c.args) >= 3
        ]
        assert any(
            "primary-m throttled" in b and "fb-1, fb-2 also unavailable" in b for b in bodies
        ), f"expected the chain story in a failure alert, got {bodies!r}"
        # The delivered detail stays bounded by the cap even with the story
        # appended (the budget trims the error part). +60 absorbs the fixed
        # "❌ Job failed:\n" / "❌ Job failed (suppressed — same error):\n"
        # prefixes around exc_detail — the bound under test is exc_detail's.
        from kiro_crew.slack.gateway import _CRON_FAILURE_DETAIL_CAP

        for b in bodies:
            assert len(b) <= _CRON_FAILURE_DETAIL_CAP + 60, f"unbounded alert body: {len(b)} chars"
        # The Slack DM — the surface a user actually gets paged on — carries
        # the story too.
        slack_texts = [str(c.args[1]) for c in gw.slack.post_message.call_args_list]
        assert any(
            "primary-m throttled" in t and "also unavailable" in t for t in slack_texts
        ), f"expected the chain story in the Slack alert, got {slack_texts!r}"
        # The second run took the dup-suppress path (story-free hash), with a
        # bounded body that still carries its own walk.
        assert any("suppressed — same error" in b for b in bodies), bodies
        assert any("fb-2 also unavailable" in b for b in bodies), bodies


class TestCronUsageRow:
    """Every model-spending cron turn appends exactly one usage row
    tagged surface='cron'; the zero-token script/command modes append none."""

    @pytest.mark.asyncio
    async def test_llm_cron_persists_usage_row_with_surface(self):
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        # Patch gateway's own bindings: the imports are at module scope there,
        # so patching the source module would not be seen by the call site.
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(1234, 200000)),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job)

        persist.assert_awaited_once()
        kwargs = persist.await_args.kwargs
        assert kwargs["surface"] == "cron"
        assert kwargs["provider"] == "acp"
        assert kwargs["context_used"] == 1234
        assert kwargs["context_window"] == 200000

    @pytest.mark.asyncio
    async def test_cron_row_records_resolved_agent_not_requested(self):
        """The agent that served the turn wins over the configured alias."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-opus-4-8")

        persist = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(10, 100)),
                create=True,
            ),
            patch(
                "kiro_crew.slack.gateway.read_effective_agent",
                MagicMock(return_value="kirocrew"),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job)

        persist.assert_awaited_once()
        assert persist.await_args.kwargs["agent"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_downgraded_cron_does_not_record_rejected_model(self):
        """A model that was refused never ran, so it must not be attributed."""
        gw = _make_gw_for_llm()
        job = _make_llm_job(model="claude-nonexistent-9")
        provider_mock = MagicMock()

        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model 'claude-nonexistent-9' not found")
            return (provider_mock, True, False)

        persist = AsyncMock()
        with (
            patch("kiro_crew.slack.gateway.persist_token_record_async", persist),
            patch(
                "kiro_crew.slack.gateway.read_context_tokens",
                MagicMock(return_value=(10, 100)),
                create=True,
            ),
        ):
            await _run_llm_callback(gw, job, get_or_create_side_effect=_side_effect)

        persist.assert_awaited_once()
        # Positional arg 1 is the model; blank defers to model_source, which
        # reports the model that actually served the turn.
        assert persist.await_args.args[1] == ""

    @pytest.mark.asyncio
    async def test_script_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_script_job()

        persist = AsyncMock()
        with patch("kiro_crew.slack.gateway.persist_token_record_async", persist):
            result, mock_run = await _run_script_callback(gw, job, {"status": "ok"})

        persist.assert_not_awaited()
        # The script did run, so the missing row is not a skipped fire.
        assert mock_run.call_count == 1
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_command_cron_writes_no_usage_row(self):
        gw = _make_gw()
        job = _make_command_job()

        persist = AsyncMock()
        with patch("kiro_crew.slack.gateway.persist_token_record_async", persist):
            result, mock_run = await _run_command_callback(
                gw, job, {"status": "ok", "output": "hello\n", "exit_code": 0}
            )

        persist.assert_not_awaited()
        # The command did run, so the missing row is not a skipped fire.
        assert mock_run.call_count == 1
        assert result == "hello\n"
