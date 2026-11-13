"""Phase 3a: surface roots. Every engine roots its turns with its trigger, the same way.

``tracing/surfaces.py`` is the one helper; each engine test streams a turn through
``turns.traced`` from inside the engine's real entry point, standing in for the
provider's own stream (layer 1).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, TurnUsage
from kiro_crew.testing.wait import default_timeout
from kiro_crew.tracing import correlation, ids, provider, runs, surfaces, turns

NS = "00112233445566778899aabbccddeeff"
KEY = b"k" * 32


class _Source:
    def __init__(self, acp_session_id="acp-1", session_key="cron:job-1"):
        self.session_id = acp_session_id
        self._session_key = session_key
        self._model = "auto"
        self._resolved_model_id = ""
        self.active_agent = "kirocrew-default"


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, KEY))
    yield exporter
    turns.reset_for_testing()
    runs.reset_for_testing()
    provider.reset_for_testing()


def _complete(credits=0.0):
    return AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage(credits=credits))


async def _events(*events):
    for event in events:
        yield event


async def _turn(source=None, credits=1.5):
    """One traced turn, the way a provider's stream runs it."""
    async for _ in turns.traced(source or _Source(), _events(_complete(credits))):
        pass


def _named(exporter, prefix):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def _hex(value, width):
    return format(value, f"0{width}x")


def _parent(span):
    return _hex(span.parent.span_id, 16) if span.parent is not None else ""


# ── the helper ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_surface_turn_roots_itself_with_its_trigger_and_channel(spans):
    with surfaces.root(surfaces.TRIGGER_CHANNEL, channel="slack"):
        await _turn()
    (turn,) = _named(spans, "invoke_agent")
    turn_id = turn.attributes["kirocrew.turn_id"]
    assert turn.parent is None
    assert _hex(turn.context.trace_id, 32) == ids.turn_trace_id(NS, turn_id)
    for span in spans.get_finished_spans():
        assert span.attributes["kirocrew.trigger"] == "channel"
        assert span.attributes["kirocrew.channel"] == "slack"


@pytest.mark.asyncio
async def test_every_prompt_inside_one_root_is_the_next_attempt_of_the_same_turn(spans):
    with surfaces.root(surfaces.TRIGGER_BACKGROUND):
        await _turn()
        await _turn()
    first, second = sorted(
        _named(spans, "invoke_agent"), key=lambda s: s.attributes["kirocrew.attempt"]
    )
    assert [first.attributes["kirocrew.attempt"], second.attributes["kirocrew.attempt"]] == [1, 2]
    assert first.context.trace_id == second.context.trace_id
    assert "kirocrew.channel" not in first.attributes


@pytest.mark.asyncio
async def test_a_turn_after_its_root_closes_is_not_part_of_it(spans):
    with surfaces.root(surfaces.TRIGGER_WEBHOOK):
        await _turn()
    await _turn()
    inside, outside = _named(spans, "invoke_agent")
    assert inside.context.trace_id != outside.context.trace_id
    assert "kirocrew.trigger" not in outside.attributes


@pytest.mark.asyncio
async def test_an_inner_root_is_its_own_turn_not_an_attempt_of_the_outer_one(spans):
    token = turns.bind_turn_root("a" * 32)
    try:
        await _turn()
        with surfaces.root(surfaces.TRIGGER_BACKGROUND):
            await _turn()
        await _turn()
    finally:
        turns.release_turn_root(token)
    outer = [s for s in _named(spans, "invoke_agent") if "kirocrew.trigger" not in s.attributes]
    (inner,) = [s for s in _named(spans, "invoke_agent") if "kirocrew.trigger" in s.attributes]
    assert sorted(s.attributes["kirocrew.attempt"] for s in outer) == [1, 2]
    assert inner.attributes["kirocrew.attempt"] == 1
    assert inner.context.trace_id != outer[0].context.trace_id


@pytest.mark.asyncio
async def test_a_run_root_anchors_its_turns_and_rolls_up_their_spend(spans):
    with surfaces.root(
        surfaces.TRIGGER_CRON,
        run_name="agent",
        run_key=("job-1", "run-1"),
        attrs={"kirocrew.cron.job_id": "job-1", "kirocrew.cron.job_kind": "agent"},
    ):
        await _turn(credits=1.5)
    (anchor,) = _named(spans, "cron agent")
    (turn,) = _named(spans, "invoke_agent")
    (outcome,) = _named(spans, "cron outcome")
    assert anchor.parent is None
    assert _hex(anchor.context.span_id, 16) == ids.span_id(NS, "cron", "job-1", "run-1", 1)
    assert _hex(anchor.context.trace_id, 32) == ids.trace_id(NS, "cron", "job-1", "run-1", 1)
    for span in (turn, outcome):
        assert span.context.trace_id == anchor.context.trace_id
        assert _parent(span) == _hex(anchor.context.span_id, 16)
    assert outcome.name == "cron outcome finished"
    assert outcome.attributes["kirocrew.usage.credits_total"] == 1.5
    assert outcome.attributes["kirocrew.usage.turns_total"] == 1
    assert anchor.attributes["kirocrew.cron.job_id"] == "job-1"
    assert anchor.attributes["kirocrew.cron.job_kind"] == "agent"
    assert turn.attributes["kirocrew.trigger"] == "cron"


@pytest.mark.asyncio
async def test_a_run_with_a_step_has_a_step_span_and_no_chat_span(spans):
    with surfaces.root(
        surfaces.TRIGGER_CRON, run_name="script", run_key=("job-2", "run-1"), step="run script"
    ):
        pass
    (anchor,) = _named(spans, "cron script")
    (step,) = _named(spans, "cron run script")
    assert _parent(step) == _hex(anchor.context.span_id, 16)
    assert step.attributes["kirocrew.run.status"] == "finished"
    assert not _named(spans, "chat") and not _named(spans, "invoke_agent")
    assert _named(spans, "cron outcome finished")


@pytest.mark.parametrize(
    "exc, status", [(RuntimeError("boom"), "failed"), (asyncio.CancelledError(), "cancelled")]
)
@pytest.mark.asyncio
async def test_a_run_that_raises_says_how_it_ended(spans, exc, status):
    from opentelemetry.trace import StatusCode

    with pytest.raises(type(exc)):
        with surfaces.root(
            surfaces.TRIGGER_CRON, run_name="command", run_key=("job-3", "r"), step="run command"
        ):
            raise exc
    (step,) = _named(spans, "cron run command")
    assert step.status.status_code == StatusCode.ERROR
    assert step.attributes["kirocrew.run.status"] == status
    assert _named(spans, f"cron outcome {status}")


@pytest.mark.asyncio
async def test_a_run_status_set_in_the_block_lands_on_its_outcome(spans):
    with surfaces.root(surfaces.TRIGGER_CRON, run_name="agent", run_key=("j", "r")) as surface:
        surface.status = surfaces.STATUS_FAILED
    assert _named(spans, "cron outcome failed")


def test_an_unknown_trigger_is_refused():
    with pytest.raises(ValueError):
        with surfaces.root("dashboard"):
            pass
    with pytest.raises(ValueError):
        surfaces.rooted("dashboard")


@pytest.mark.asyncio
async def test_a_rooted_function_runs_each_call_in_its_own_root(spans):
    @surfaces.rooted(surfaces.TRIGGER_TASK)
    async def step(n):
        for _ in range(n):
            await _turn()
        return n

    assert await step(2) == 2
    assert await step(1) == 1
    attempts = sorted(
        (s.context.trace_id, s.attributes["kirocrew.attempt"])
        for s in _named(spans, "invoke_agent")
    )
    assert len({trace for trace, _ in attempts}) == 2
    assert sorted(a for _, a in attempts) == [1, 1, 2]
    assert {s.attributes["kirocrew.trigger"] for s in _named(spans, "invoke_agent")} == {"task"}


def test_off_a_root_imports_no_sdk(tmp_path):
    program = textwrap.dedent("""
        import asyncio, sys
        from kiro_crew.tracing import provider, surfaces

        @surfaces.rooted(surfaces.TRIGGER_TASK)
        async def step():
            return 1

        async def main():
            with surfaces.root(
                surfaces.TRIGGER_CRON, run_name="script", run_key=("j", "r"), step="run script"
            ) as surface:
                assert surface.run is None
            assert await step() == 1

        asyncio.run(main())
        assert provider.peek() is None
        assert "opentelemetry" not in sys.modules, "OpenTelemetry imported while off"
        print("ok")
        """)
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "KIROCREW_HOME": str(tmp_path)},
        timeout=default_timeout(),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


# ── the engines ───────────────────────────────────────────────────────────


def _cron(tmp_path, on_job, *, script=""):
    from kiro_crew.cron import CronService
    from kiro_crew.cron_service.claims import _RunClaim

    svc = CronService(base_dir=tmp_path)
    svc._on_job = on_job
    job = svc.add_job("nightly digest", "summarise", every_secs=900)
    job.script = script
    return svc, job, _RunClaim(trigger="manual", claimed_at=time.time())


@pytest.mark.asyncio
async def test_an_agent_cron_run_is_one_anchored_trace(spans, tmp_path):
    async def on_job(job):
        await _turn(_Source(session_key=f"cron:{job.id}"))

    svc, job, claim = _cron(tmp_path, on_job)
    await svc._execute_with_timeout(job, claim)
    (anchor,) = _named(spans, "cron agent")
    (turn,) = _named(spans, "invoke_agent")
    assert _hex(anchor.context.span_id, 16) == ids.span_id(NS, "cron", job.id, claim.marker_run, 1)
    assert _parent(turn) == _hex(anchor.context.span_id, 16)
    assert turn.attributes["kirocrew.trigger"] == "cron"
    assert anchor.attributes["kirocrew.cron.job_id"] == job.id
    assert _named(spans, "cron outcome finished")
    assert "nightly" not in " ".join(s.name for s in spans.get_finished_spans())
    # R6: one session.id across the run's trace, the job's own session.
    session = correlation.keyed_hash(KEY, f"cron:{job.id}")
    assert {s.attributes.get("session.id") for s in spans.get_finished_spans()} == {session}


@pytest.mark.asyncio
async def test_a_retried_cron_run_is_attempt_two_under_the_same_anchor(spans, tmp_path):
    async def on_job(job):
        # The transient-retry shape of ``cron_turn``: the callback re-enters itself.
        await _turn(_Source(session_key=f"cron:{job.id}"))
        if not getattr(job, "_transient_attempts", 0):
            job._transient_attempts = 1
            await on_job(job)

    svc, job, claim = _cron(tmp_path, on_job)
    await svc._execute_with_timeout(job, claim)
    (anchor,) = _named(spans, "cron agent")
    attempts = _named(spans, "invoke_agent")
    assert sorted(s.attributes["kirocrew.attempt"] for s in attempts) == [1, 2]
    assert {_parent(s) for s in attempts} == {_hex(anchor.context.span_id, 16)}
    (outcome,) = _named(spans, "cron outcome")
    assert outcome.attributes["kirocrew.usage.turns_total"] == 2
    assert job.last_retry_count == 1


@pytest.mark.asyncio
async def test_a_script_cron_run_has_a_run_span_and_no_chat_span(spans, tmp_path):
    async def on_job(job):
        job.last_status = "error"

    svc, job, claim = _cron(tmp_path, on_job, script="crons/digest.py:main")
    await svc._execute_with_timeout(job, claim)
    (anchor,) = _named(spans, "cron script")
    (step,) = _named(spans, "cron run script")
    assert _parent(step) == _hex(anchor.context.span_id, 16)
    assert step.attributes["kirocrew.cron.job_kind"] == "script"
    assert not _named(spans, "chat")
    assert _named(spans, "cron outcome failed")


@pytest.mark.asyncio
async def test_a_cron_run_its_deadline_ends_is_traced_as_failed(spans, tmp_path, monkeypatch):
    from opentelemetry.trace import StatusCode

    async def deadline(coro, timeout):
        # What ``wait_for`` raises once the deadline cancels the run.
        coro.close()
        raise asyncio.TimeoutError

    async def on_job(job):
        raise AssertionError("never reached")

    svc, job, claim = _cron(tmp_path, on_job, script="crons/digest.py:main")
    monkeypatch.setattr(asyncio, "wait_for", deadline)
    await svc._execute_with_timeout(job, claim)
    assert job.last_status == "error"
    (step,) = _named(spans, "cron run script")
    assert step.status.status_code == StatusCode.ERROR
    assert step.attributes["kirocrew.run.status"] == "failed"
    assert _named(spans, "cron outcome failed")
    assert not _named(spans, "cron outcome cancelled")


@pytest.mark.asyncio
async def test_a_cron_run_the_reaper_kills_is_traced_as_failed(spans, tmp_path):
    async def on_job(job):
        # ``_force_reap`` marks the claim it took, then cancels the run's task.
        claim.reaped = True
        raise asyncio.CancelledError

    svc, job, claim = _cron(tmp_path, on_job, script="crons/digest.py:main")
    with pytest.raises(asyncio.CancelledError):
        await svc._execute_with_timeout(job, claim)
    (step,) = _named(spans, "cron run script")
    assert step.attributes["kirocrew.run.status"] == "failed"
    assert _named(spans, "cron outcome failed")


@pytest.mark.asyncio
async def test_a_run_whose_step_cannot_start_leaves_no_rollup_open(spans, monkeypatch):
    real = provider.start_span

    def failing(state, name, **kw):
        if name.startswith("cron run"):
            raise RuntimeError("exporter gone")
        return real(state, name, **kw)

    monkeypatch.setattr(provider, "start_span", failing)
    with surfaces.root(
        surfaces.TRIGGER_CRON, run_name="script", run_key=("j", "r"), step="run script"
    ) as surface:
        assert surface.run is None
    assert turns._rollups == {}


@pytest.mark.asyncio
async def test_a_cron_run_that_never_started_is_skipped_not_failed(spans, tmp_path):
    from opentelemetry.trace import StatusCode

    async def on_job(job):
        # A fire-time gate deferring the run (``cron_dispatch``).
        job.last_status = "error"
        job.run_never_started = True

    svc, job, claim = _cron(tmp_path, on_job, script="crons/digest.py:main")
    await svc._execute_with_timeout(job, claim)
    (step,) = _named(spans, "cron run script")
    assert step.status.status_code != StatusCode.ERROR
    assert step.attributes["kirocrew.run.status"] == "skipped"
    assert _named(spans, "cron outcome skipped")
    assert not _named(spans, "cron outcome failed")


async def _channel_turn(audit_session_key, channel_type=""):
    """One TurnDriver turn whose renderer reports *channel_type*."""
    from kiro_crew.messaging import Renderer, TransportCapabilities
    from kiro_crew.messaging.driver import TurnDriver

    class _Quiet(Renderer):
        def __init__(self):
            super().__init__(TransportCapabilities())
            self.channel_type = channel_type

        async def on_text_chunk(self, text):
            pass

        async def on_thinking(self, text):
            pass

        async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
            pass

        async def on_tool_result(self, tool_call_id, tool_status):
            pass

        async def on_prompt_choice(
            self, options, request_id, tool_title="", tool_purpose="", tool_input=""
        ):
            pass

        async def on_compaction(self, pct):
            pass

        async def on_done(self, stop_reason=""):
            pass

        async def on_steer_consumed(self, summary=""):
            pass

    class _Provider:
        def stream(self, message):
            text = AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi")
            return turns.traced(_Source(session_key=audit_session_key), _events(text, _complete()))

    driver = TurnDriver(_Provider(), _Quiet(), audit_session_key=audit_session_key)
    await driver.run("hello")


@pytest.mark.asyncio
async def test_a_channel_turn_roots_with_its_channel(spans):
    await _channel_turn("telegram:42")
    (turn,) = _named(spans, "invoke_agent")
    assert turn.attributes["kirocrew.trigger"] == "channel"
    assert turn.attributes["kirocrew.channel"] == "telegram"


@pytest.mark.asyncio
async def test_a_reply_in_a_dashboard_linked_thread_keeps_its_channel(spans):
    # The turn runs under the dashboard session's key; the renderer is Slack's.
    await _channel_turn("dashboard:chat-7", channel_type="slack")
    (turn,) = _named(spans, "invoke_agent")
    assert turn.attributes["kirocrew.channel"] == "slack"


@pytest.mark.asyncio
async def test_a_channel_label_outside_the_vocabulary_is_never_used(spans):
    await _channel_turn("dashboard:chat-7", channel_type="dashboard")
    (turn,) = _named(spans, "invoke_agent")
    assert "kirocrew.channel" not in turn.attributes


@pytest.mark.usefixtures("ample_host_free_memory")
@pytest.mark.asyncio
async def test_a_background_one_liner_roots_with_its_trigger(spans):
    from kiro_crew.llm_helpers import run_bg_oneliner

    class _Session:
        last_prompt_stats = None

        def prompt(self, _prompt, *, allow_image=True):
            return turns.traced(_Source(session_key="_bg"), _events(_complete()))

        async def destroy(self):
            pass

    sessions = MagicMock()
    sessions.get_bg_session = AsyncMock(return_value=_Session())
    await run_bg_oneliner(sessions, "title this")
    (turn,) = _named(spans, "invoke_agent")
    assert turn.attributes["kirocrew.trigger"] == "background"


@pytest.mark.usefixtures("ample_host_free_memory")
@pytest.mark.asyncio
async def test_a_background_turn_roots_with_its_trigger(spans, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import usage
    from kiro_crew.llm_helpers import background_turn

    class _Sessions:
        async def get_or_create(self, key, **_kw):
            return SimpleNamespace(last_prompt_stats=None), False, False

        def release(self, key):
            pass

        async def recycle_background(self, **_kw):
            pass

    monkeypatch.setattr(usage, "persist_token_record_async", AsyncMock())
    async with background_turn(_Sessions(), task="summary"):
        await _turn(_Source(session_key="_bg"))
    await _turn()
    inside, after = _named(spans, "invoke_agent")
    assert inside.attributes["kirocrew.trigger"] == "background"
    assert "kirocrew.trigger" not in after.attributes


class _Seen(Exception):
    """Raised by the recording root, so the engine body never runs."""


@pytest.fixture
def seen_triggers(monkeypatch):
    seen: list[str] = []

    def recording_root(trigger, **_kw):
        seen.append(trigger)
        raise _Seen

    monkeypatch.setattr(surfaces, "root", recording_root)
    return seen


@pytest.mark.asyncio
async def test_a_webhook_turn_roots_with_its_trigger(seen_triggers):
    from kiro_crew.dashboard.handlers import hooks

    with pytest.raises(_Seen):
        await hooks._run_hook_inner(MagicMock(), "hook:k", "payload", None)
    assert seen_triggers == ["webhook"]


@pytest.mark.asyncio
async def test_a_task_runner_step_roots_with_its_trigger(seen_triggers, tmp_path):
    from kiro_crew import task_executor

    with pytest.raises(_Seen):
        await task_executor.execute_task(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            None,
            "",
            None,
            False,
            None,
            tmp_path,
            AsyncMock(),
        )
    assert seen_triggers == ["task"]


@pytest.mark.asyncio
async def test_a_task_runner_self_review_roots_with_its_trigger(seen_triggers):
    from kiro_crew import task_executor

    with pytest.raises(_Seen):
        await task_executor.self_review(MagicMock(), MagicMock(), MagicMock(), "")
    assert seen_triggers == ["task"]


@pytest.mark.asyncio
async def test_a_heartbeat_task_roots_with_its_trigger(seen_triggers, monkeypatch):
    import kiro_crew.slack.gateway as gw_mod

    started = {}

    async def fake_start(self):
        started["on_task"] = self._on_task

    monkeypatch.setattr(gw_mod.HeartbeatService, "start", fake_start)
    orch = gw_mod.GatewayOrchestrator.__new__(gw_mod.GatewayOrchestrator)
    orch.sessions = MagicMock()
    orch.ctx_builder = MagicMock()
    orch.consolidator = None
    await orch._init_heartbeat()
    with pytest.raises(_Seen):
        await started["on_task"]("check the enclosure", "")
    assert seen_triggers == ["heartbeat"]


@pytest.mark.asyncio
async def test_each_agent_of_a_cron_sequence_is_its_own_turn_of_the_run(spans, tmp_path):
    """The gateway's real ``_cron_callback``: an agent sequence streams in one task."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from cron_gateway_integration_helpers import _make_gw_for_llm, _make_llm_job

    from kiro_crew.cron import CronService
    from kiro_crew.cron_service.claims import _RunClaim

    async def stream_and_collect(client, _message, **_kw):
        source = SimpleNamespace(session_id=client.acp_id, _session_key="cron:seq")
        async for _ in turns.traced(source, _events(_complete(1.0))):
            pass
        return "done"

    gw = _make_gw_for_llm()
    clients = iter([MagicMock(acp_id="acp-a"), MagicMock(acp_id="acp-b")])
    gw.sessions.get_or_create = AsyncMock(
        side_effect=lambda *_a, **_k: (next(clients), True, False)
    )
    job = _make_llm_job(id="seq", agent_id="", agent_sequence=["seq-a", "seq-b"])
    captured = {}

    def capture_cron(on_job=None, **_kw):
        captured["cb"] = on_job
        svc = MagicMock()
        svc.start = AsyncMock()
        svc.remove_job_async = AsyncMock(return_value=True)
        return svc

    with (
        patch("kiro_crew.slack.gateway.CronService") as cron_cls,
        patch(
            "kiro_crew.slack.gateway.run_in_embed_pool",
            AsyncMock(return_value=("full prompt", None)),
        ),
        patch("kiro_crew.slack.gateway.stream_and_collect", stream_and_collect),
        patch("kiro_crew.slack.gateway.sel"),
        patch("kiro_crew.slack.gateway.build_cron_session_context") as ctx,
        patch("kiro_crew.cron_service.agent_presence.cron_agent_missing", return_value=False),
    ):
        ctx.return_value = (f"cron:{job.id}", job.message)
        cron_cls.create = AsyncMock(side_effect=capture_cron)
        await gw._init_cron()
        cron = CronService(base_dir=tmp_path)
        cron._on_job = captured["cb"]
        await cron._execute_with_timeout(job, _RunClaim(trigger="manual", claimed_at=0.0))

    (anchor,) = _named(spans, "cron agent")
    agents = _named(spans, "invoke_agent")
    assert len(agents) == 2
    assert [s.attributes["kirocrew.attempt"] for s in agents] == [1, 1]
    assert len({s.attributes["kirocrew.turn_id"] for s in agents}) == 2
    assert {_parent(s) for s in agents} == {_hex(anchor.context.span_id, 16)}
    (outcome,) = _named(spans, "cron outcome")
    assert outcome.attributes["kirocrew.usage.turns_total"] == 2
