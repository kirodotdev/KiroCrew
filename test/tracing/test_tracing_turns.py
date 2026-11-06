"""A traced ACP turn: the switch, the span tree, tool spans, usage and content safety."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, TurnUsage
from kiro_crew.metrics import tool_calls
from kiro_crew.testing.wait import default_timeout, wait_until
from kiro_crew.tracing import ids, provider, turns

NS = "00112233445566778899aabbccddeeff"
TURN_ID = "3f9a1c2e4b5d6e7f8091a2b3c4d5e6f7"
SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"  # split: not a credential-shaped literal in the repo
PROMPT = "please summarise my private notes"


class _Source:
    """The attributes a provider exposes that the trace reads."""

    def __init__(self, acp_session_id="acp-1", session_key="dashboard_chat-7", model="auto"):
        self.session_id = acp_session_id
        self._session_key = session_key
        self._model = model
        self._resolved_model_id = ""
        self.active_agent = "kirocrew-default"


@pytest.fixture
def spans():
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider.install_for_testing(provider.new_state(SimpleSpanProcessor(exporter), NS, b"k" * 32))
    yield exporter
    turns.reset_for_testing()
    provider.reset_for_testing()


async def _events(*events, raise_exc=None):
    for event in events:
        yield event
    if raise_exc is not None:
        raise raise_exc


def _complete(**usage):
    return AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=TurnUsage(**usage))


async def _drain(source, stream):
    seen = []
    async for event in turns.traced(source, stream):
        seen.append(event)
    return seen


def _by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


def _hex(value: int, width: int) -> str:
    return format(value, f"0{width}x")


# ── the switch ────────────────────────────────────────────────────────────


def test_off_by_default_hands_the_stream_back_untouched():
    provider.reset_for_testing()
    stream = _events()
    assert provider.current() is None
    assert turns.traced(_Source(), stream) is stream
    assert turns.bind_turn_root("") is None
    provider.reset_for_testing()


def test_off_imports_no_sdk_and_starts_no_thread(tmp_path):
    program = textwrap.dedent("""
        import asyncio, sys, threading
        from kiro_crew.tracing import provider, turns
        from kiro_crew.metrics import tool_calls

        async def events():
            yield 1

        class S:
            session_id = "a"

        async def main():
            async for _ in turns.traced(S(), events()):
                pass
            tool_calls.note_tool_call_started("t1", kind="read", scope="a")
            tool_calls.record_tool_call_finished("t1", status="completed", scope="a")

        asyncio.run(main())
        assert provider.current() is None
        assert "opentelemetry.sdk.trace" not in sys.modules, "SDK imported while off"
        assert not [t for t in threading.enumerate() if "Otel" in t.name]
        print("ok")
        """)
    env = {**os.environ, "KIROCREW_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=default_timeout(),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


def test_peek_never_reads_the_switch_itself():
    provider.reset_for_testing()
    assert provider.peek() is None
    assert provider.resolved() is False
    provider.reset_for_testing()


def test_a_config_double_never_turns_tracing_on(monkeypatch):
    from unittest.mock import MagicMock

    from kiro_crew.config.loader import KiroCrewConfig

    # A test patching load() to return a MagicMock has a truthy traces_enabled.
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: MagicMock()))
    provider.reset_for_testing()
    try:
        assert provider.current() is None
    finally:
        provider.reset_for_testing()


def test_an_unreadable_config_or_vault_degrades_quietly(monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.secrets.vault import SecretVault

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(SecretVault, "derive_subkey", boom)
    assert provider._hash_key() is None
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
    assert provider._build() is None and provider._built_inputs is None


def test_watch_config_subscribes_once_and_its_applier_runs_off_loop(monkeypatch):
    from kiro_crew.config import live

    calls = []
    monkeypatch.setattr(provider, "_config_sub", None)
    monkeypatch.setattr(live, "subscribe", lambda *p, **kw: calls.append((p, kw)) or object())
    provider.reset_for_testing()
    provider.watch_config()
    provider.watch_config()
    assert [(p, kw["name"]) for p, kw in calls] == [(("telemetry",), "tracing")]
    wait_until(provider.resolved)  # the boot read ran on a worker thread
    provider.reset_for_testing()  # unresolved: the applier has nothing to compare
    asyncio.run(calls[0][1]["callback"](None))
    assert provider._resolved is False


def test_a_failed_build_leaves_no_export_thread(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telemetry": {"traces_enabled": True}}), encoding="utf-8"
    )
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "namespace").write_text("garbage", encoding="utf-8")
    before = {t for t in threading.enumerate() if "Otel" in t.name}
    provider.reset_for_testing()
    try:
        assert provider.current() is None
        assert {t for t in threading.enumerate() if "Otel" in t.name} == before
    finally:
        provider.reset_for_testing()


def test_only_a_write_to_a_tracer_input_rebuilds_it(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"telemetry": {"traces_enabled": True}}), encoding="utf-8")
    provider.reset_for_testing()
    try:
        state = provider.current()
        assert state is not None
        config.write_text(
            json.dumps({"telemetry": {"traces_enabled": True, "beacon_enabled": False}}),
            encoding="utf-8",
        )
        provider._apply_config_change()
        assert provider.peek() is state  # a streaming turn keeps its provider
        config.write_text(json.dumps({"telemetry": {"traces_enabled": False}}), encoding="utf-8")
        provider._apply_config_change()
        assert provider.current() is None
    finally:
        provider.reset_for_testing()


def test_turning_tracing_off_during_a_slow_first_build_still_turns_it_off(tmp_path, monkeypatch):
    import threading

    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"telemetry": {"traces_enabled": True}}), encoding="utf-8")
    release, building = threading.Event(), threading.Event()

    def slow_build(cfg):
        building.set()
        release.wait(default_timeout())
        return provider.new_state(SimpleSpanProcessor(InMemorySpanExporter()), NS, None)

    monkeypatch.setattr(provider, "_build_live", slow_build)
    provider.reset_for_testing()
    try:
        resolver = threading.Thread(target=provider.current)  # what traced() awaits
        resolver.start()
        assert building.wait(default_timeout())
        config.write_text(json.dumps({"telemetry": {"traces_enabled": False}}), encoding="utf-8")
        applier = threading.Thread(target=provider._apply_config_change)
        applier.start()
        release.set()
        applier.join(default_timeout())
        resolver.join(default_timeout())
        assert provider.current() is None  # the opt-out won, not the stale build
    finally:
        release.set()
        provider.reset_for_testing()


def test_a_new_cap_is_set_in_place_and_keeps_the_tracer(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"telemetry": {"traces_enabled": True}}), encoding="utf-8")
    provider.reset_for_testing()
    try:
        state = provider.current()
        config.write_text(
            json.dumps({"telemetry": {"traces_enabled": True, "max_total_mb": 7}}),
            encoding="utf-8",
        )
        provider._apply_config_change()
        assert provider.current() is state  # the same tracer, never a second writer
        assert state.sink._shards._max_total_bytes == 7 * 1024 * 1024
    finally:
        provider.reset_for_testing()


def test_caps_set_to_zero_with_tracing_switched_off_reach_the_kept_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = tmp_path / "config.json"
    on = {"traces_enabled": True, "retention_days": 7, "max_total_mb": 5}
    config.write_text(json.dumps({"telemetry": on}), encoding="utf-8")
    provider.reset_for_testing()
    try:
        state = provider.current()
        assert state.sink._shards._retention_days == 7
        # One write: tracing off AND pruning off, while a turn may still export.
        off = {"traces_enabled": False, "retention_days": 0, "max_total_mb": 0}
        config.write_text(json.dumps({"telemetry": off}), encoding="utf-8")
        provider._apply_config_change()
        assert provider.peek() is None
        assert state.sink._shards._retention_days == 0
        assert state.sink._shards._max_total_bytes == 0
    finally:
        provider.reset_for_testing()


@pytest.mark.asyncio
async def test_turning_tracing_off_mid_turn_keeps_that_turns_spans(spans, tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telemetry": {"traces_enabled": False}}), encoding="utf-8"
    )
    state = provider.peek()

    async def stream():
        tool_calls.note_tool_call_started("t1", kind="read", scope="acp-1", tool_name="read")
        provider._apply_config_change()  # an opt-out written mid-turn
        tool_calls.record_tool_call_finished("t1", status="completed", scope="acp-1")
        yield _complete(credits=2.0)

    await _drain(_Source(), stream())
    names = [s.name for s in spans.get_finished_spans()]
    assert "invoke_agent kirocrew-default" in names and "execute_tool read" in names
    assert provider.current() is None  # new turns see the opt-out
    assert provider._built is state  # and nothing was shut down under the turn


@pytest.mark.asyncio
async def test_a_settings_write_mid_turn_keeps_that_turns_later_tool_spans(spans):
    async def stream():
        provider._apply_config_change()  # a write: new turns re-read the switch
        assert provider.peek() is None
        tool_calls.note_tool_call_started("t1", kind="read", scope="acp-1", tool_name="read")
        tool_calls.record_tool_call_finished("t1", status="completed", scope="acp-1")
        yield _complete()

    await _drain(_Source(), stream())
    assert "execute_tool read" in [s.name for s in spans.get_finished_spans()]


@pytest.mark.asyncio
async def test_the_exit_flush_writes_queued_spans(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telemetry": {"traces_enabled": True}}), encoding="utf-8"
    )
    provider.reset_for_testing()
    try:
        await asyncio.to_thread(provider.current)  # what the boot read does
        await _drain(_Source(), _events(_complete(credits=1.0)))
        await provider.flush_for_exit()
        assert provider.resolved() is False
        shards = list((tmp_path / "traces").glob("spans-*.jsonl"))
        assert shards and shards[0].read_text(encoding="utf-8").count("\n") == 4
    finally:
        provider.reset_for_testing()


def test_a_turn_before_the_switch_is_read_streams_untouched_and_untraced():
    provider.reset_for_testing()
    stream = _events(_complete())
    # No await, no read: the prompt goes out as soon as the caller iterates.
    assert turns.traced(_Source(), stream) is stream
    assert provider.resolved() is False


def test_a_traced_turn_lands_in_the_local_sink_under_its_bound_id(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telemetry": {"traces_enabled": True}}), encoding="utf-8"
    )
    provider.reset_for_testing()
    try:
        state = provider.current()  # what the boot read does
        assert state is not None
        assert state.namespace == (tmp_path / "traces" / "namespace").read_text().strip()

        async def run():
            bound = turns.bind_turn_root(TURN_ID)
            try:
                await _drain(_Source(), _events(_complete(credits=1.5)))
            finally:
                turns.release_turn_root(bound)

        asyncio.run(run())
    finally:
        provider.reset_for_testing()  # joins the batch thread and flushes
    lines = [
        json.loads(line)
        for shard in (tmp_path / "traces").glob("spans-*.jsonl")
        for line in shard.read_text(encoding="utf-8").splitlines()
    ]
    assert {line["name"] for line in lines} == {
        "invoke_agent kirocrew-default",
        "user.prompt",
        "chat",
        "turn.complete",
    }
    assert {line["context"]["trace_id"] for line in lines} == {"0x" + TURN_ID}


# ── the tree ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bound_turn_id_is_the_trace_id_and_the_tree_has_its_shape(spans):
    token = turns.bind_turn_root(TURN_ID)
    try:
        source = _Source()
        await _drain(source, _events(AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()))
    finally:
        turns.release_turn_root(token)
    by_name = _by_name(spans)
    turn = by_name["invoke_agent kirocrew-default"]
    assert _hex(turn.context.trace_id, 32) == TURN_ID
    assert turn.parent is None
    assert _hex(turn.context.span_id, 16) == ids.span_id(NS, "turn", TURN_ID, 1)
    for name, kind in (
        ("user.prompt", "user.prompt"),
        ("chat", "chat"),
        ("turn.complete", "turn.complete"),
    ):
        child = by_name[name]
        assert child.parent.span_id == turn.context.span_id
        assert _hex(child.context.span_id, 16) == ids.span_id(NS, kind, TURN_ID, 1)
    assert by_name["turn.complete"].attributes["kirocrew.stop_reason"] == "end_turn"
    attrs = turn.attributes
    assert attrs["kirocrew.turn_id"] == TURN_ID
    assert attrs["kirocrew.acp_session_id"] == "acp-1"
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["kirocrew.local.session_key"] == "dashboard_chat-7"
    assert attrs["session.id"] == attrs["kirocrew.session_key_hash"] != "dashboard_chat-7"


@pytest.mark.asyncio
async def test_the_spans_end_before_the_terminal_event_reaches_the_consumer(spans):
    stream = turns.traced(_Source(), _events(_complete(credits=1.0)))
    try:
        event = await stream.__anext__()
        assert event.kind == EVENT_COMPLETE
        # The consumer has the event but has not closed the stream: its settling
        # (billing, transcript writes) must not count as model time.
        assert {"invoke_agent kirocrew-default", "chat", "turn.complete"} <= set(_by_name(spans))
    finally:
        await stream.aclose()
    assert [s.name for s in spans.get_finished_spans()].count("chat") == 1


@pytest.mark.asyncio
async def test_a_second_stream_of_one_turn_is_attempt_two_in_the_same_trace(spans):
    token = turns.bind_turn_root(TURN_ID)
    try:
        await _drain(_Source(), _events(_complete()))
        await _drain(_Source(), _events(_complete()))
    finally:
        turns.release_turn_root(token)
    roots = [s for s in spans.get_finished_spans() if s.parent is None]
    assert [s.attributes["kirocrew.attempt"] for s in roots] == [1, 2]
    assert {_hex(s.context.trace_id, 32) for s in roots} == {TURN_ID}


@pytest.mark.asyncio
async def test_a_task_spawned_inside_the_turn_roots_its_own_trace(spans):
    token = turns.bind_turn_root(TURN_ID)
    try:
        await asyncio.create_task(_drain(_Source("acp-bg"), _events(_complete())))
    finally:
        turns.release_turn_root(token)
    (root,) = [s for s in spans.get_finished_spans() if s.parent is None]
    assert _hex(root.context.trace_id, 32) != TURN_ID
    assert root.attributes["kirocrew.attempt"] == 1


# ── tool spans ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_call_is_a_child_of_its_turn_and_mcp_names_never_show(spans):
    async def stream():
        tool_calls.note_tool_call_started("t1", kind="execute", scope="acp-1", tool_name="shell")
        tool_calls.note_tool_call_started(
            "t2", kind="read", scope="acp-1", mcp_server_name="acme-crm", tool_name="get_deal"
        )
        tool_calls.note_tool_call_started("t3", kind="read", scope="acp-1", tool_name="mcp_x_y")
        tool_calls.note_tool_call_started("t4", kind="search", scope="acp-1")
        for tcid in ("t1", "t2", "t3", "t4"):
            tool_calls.record_tool_call_finished(tcid, status="completed", scope="acp-1")
        yield _complete()

    await _drain(_Source(), stream())
    by_name = _by_name(spans)
    turn = by_name["invoke_agent kirocrew-default"]
    shell = by_name["execute_tool shell"]
    assert shell.parent.span_id == turn.context.span_id
    assert _hex(shell.context.span_id, 16) == ids.span_id(NS, "tool", "acp-1", "t1")
    assert shell.attributes["gen_ai.tool.name"] == "shell"
    assert shell.attributes["kirocrew.tool_call_id"] == "t1"
    assert shell.attributes["kirocrew.tool.outcome"] == "completed"
    tools = [s for s in spans.get_finished_spans() if s.name.startswith("execute_tool")]
    assert sorted(s.name for s in tools) == [
        "execute_tool mcp",
        "execute_tool mcp",
        "execute_tool search",
        "execute_tool shell",
    ]
    dumped = json.dumps([dict(s.attributes) for s in tools])
    assert "acme-crm" not in dumped and "get_deal" not in dumped and "mcp_x_y" not in dumped


@pytest.mark.asyncio
async def test_the_same_tool_call_id_in_two_sessions_stays_two_spans(spans):
    gate = asyncio.Event()
    started = {"acp-a": asyncio.Event(), "acp-b": asyncio.Event()}

    async def stream(scope):
        tool_calls.note_tool_call_started("same", kind="read", scope=scope, tool_name="read")
        started[scope].set()
        await gate.wait()
        tool_calls.record_tool_call_finished("same", status="completed", scope=scope)
        yield _complete()

    async def both():
        a = asyncio.create_task(_drain(_Source("acp-a"), stream("acp-a")))
        b = asyncio.create_task(_drain(_Source("acp-b"), stream("acp-b")))
        # Both calls are open at once before either finishes.
        await asyncio.wait_for(
            asyncio.gather(*(e.wait() for e in started.values())), default_timeout()
        )
        gate.set()
        await asyncio.gather(a, b)

    await both()
    tools = [s for s in spans.get_finished_spans() if s.name == "execute_tool read"]
    roots = {s.context.span_id: s for s in spans.get_finished_spans() if s.parent is None}
    assert len(tools) == 2
    assert {roots[t.parent.span_id].attributes["kirocrew.acp_session_id"] for t in tools} == {
        "acp-a",
        "acp-b",
    }
    assert len({t.context.span_id for t in tools}) == 2


@pytest.mark.asyncio
async def test_a_replayed_start_for_an_ended_call_emits_nothing(spans):
    async def stream():
        tool_calls.note_tool_call_started("t1", kind="read", scope="acp-1", tool_name="read")
        tool_calls.record_tool_call_finished("t1", status="completed", scope="acp-1")
        # session/load replays the same frames
        tool_calls.note_tool_call_started("t1", kind="read", scope="acp-1", tool_name="read")
        tool_calls.record_tool_call_finished("t1", status="completed", scope="acp-1")
        yield _complete()

    await _drain(_Source(), stream())
    assert [s.name for s in spans.get_finished_spans()].count("execute_tool read") == 1


@pytest.mark.asyncio
async def test_an_oversized_tool_call_id_is_not_retained(spans):
    huge = "x" * 100_000

    async def stream():
        tool_calls.note_tool_call_started(huge, kind="read", scope="acp-1", tool_name="read")
        tool_calls.note_tool_call_started("ok", kind="read", scope="acp-1", tool_name="read")
        tool_calls.record_tool_call_finished(huge, status="completed", scope="acp-1")
        tool_calls.record_tool_call_finished("ok", status="completed", scope="acp-1")
        yield _complete()

    await _drain(_Source(), stream())
    assert [k for k in turns._ended_tools if huge in k] == []
    assert [s.name for s in spans.get_finished_spans()].count("execute_tool read") == 1


@pytest.mark.asyncio
async def test_a_non_string_tool_call_id_is_ignored_and_never_raises(spans):
    async def stream():
        tool_calls.note_tool_call_started("ok", kind="read", scope="acp-1", tool_name="read")
        # A backend sending a number where the protocol says string.
        tool_calls.note_tool_call_started(1234, kind="read", scope="acp-1")
        tool_calls.record_tool_call_finished(1234, status="completed", scope="acp-1")
        assert turns._bounded(["acp-1"], "x") is False
        tool_calls.record_tool_call_finished("ok", status="completed", scope="acp-1")
        yield _complete()

    await _drain(_Source(), stream())
    assert [s.name for s in spans.get_finished_spans()].count("execute_tool read") == 1


@pytest.mark.asyncio
async def test_a_tool_with_no_open_turn_is_not_traced_and_an_unfinished_one_ends_with_it(spans):
    tool_calls.note_tool_call_started("orphan", kind="read", scope="acp-1")
    tool_calls.record_tool_call_finished("orphan", status="completed", scope="acp-1")

    async def stream():
        tool_calls.note_tool_call_started("open", kind="read", scope="acp-1", tool_name="read")
        yield _complete()

    await _drain(_Source(), stream())
    (tool,) = [s for s in spans.get_finished_spans() if s.name.startswith("execute_tool")]
    assert tool.attributes["kirocrew.tool_call_id"] == "open"
    assert "kirocrew.tool.outcome" not in tool.attributes


# ── usage ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_kiro_turn_carries_credits_and_no_token_keys(spans):
    await _drain(_Source(), _events(_complete(credits=0.96, num_turns=3)))
    by_name = _by_name(spans)
    chat, turn = by_name["chat"], by_name["invoke_agent kirocrew-default"]
    for span in (chat, turn):
        assert span.attributes["kirocrew.usage.credits"] == 0.96
        assert span.attributes["kirocrew.usage.model_calls"] == 3
        assert span.attributes["kirocrew.usage.complete"] is True
        assert span.attributes["gen_ai.request.model"] == "auto"
        assert "gen_ai.response.model" not in span.attributes
        assert not [k for k in span.attributes if k.startswith("gen_ai.usage.")]
    assert json.loads(chat.attributes["langfuse.observation.usage_details"]) == {"credits": 0.96}
    assert chat.attributes["langfuse.observation.type"] == "generation"
    assert chat.attributes["kirocrew.chat.aggregate"] is True
    assert "langfuse.observation.type" not in turn.attributes


@pytest.mark.asyncio
async def test_a_claude_turn_carries_tokens_cost_and_the_resolved_model(spans):
    source = _Source(model="sonnet-alias")
    source._resolved_model_id = "resolved-model-1"
    await _drain(source, _events(_complete(input_tokens=120, output_tokens=40, cost_usd=0.0097)))
    chat = _by_name(spans)["chat resolved-model-1"]
    assert chat.attributes["gen_ai.usage.input_tokens"] == 120
    assert chat.attributes["gen_ai.usage.output_tokens"] == 40
    assert chat.attributes["kirocrew.usage.cost_usd"] == 0.0097
    assert chat.attributes["gen_ai.response.model"] == "resolved-model-1"
    assert json.loads(chat.attributes["langfuse.observation.usage_details"]) == {
        "input": 120,
        "output": 40,
    }
    assert json.loads(chat.attributes["langfuse.observation.cost_details"]) == {"total": 0.0097}


@pytest.mark.asyncio
async def test_a_cancelled_turn_is_incomplete_and_not_an_error(spans):
    with pytest.raises(asyncio.CancelledError):
        await _drain(_Source(), _events(raise_exc=asyncio.CancelledError()))
    turn = _by_name(spans)["invoke_agent kirocrew-default"]
    assert turn.attributes["kirocrew.usage.complete"] is False
    assert "error.type" not in turn.attributes
    assert "turn.complete" not in _by_name(spans)


class _Stats:
    def __init__(self, credits):
        self.credits = credits

    def to_turn_usage(self):
        return TurnUsage(credits=self.credits)


@pytest.mark.asyncio
async def test_a_turn_cut_before_its_prompt_never_bills_the_previous_turn(spans):
    source = _Source()
    source.last_prompt_stats = _Stats(5.0)  # what the previous turn left behind
    with pytest.raises(asyncio.CancelledError):
        await _drain(source, _events(raise_exc=asyncio.CancelledError()))
    turn = _by_name(spans)["invoke_agent kirocrew-default"]
    assert "kirocrew.usage.credits" not in turn.attributes


@pytest.mark.asyncio
async def test_a_turn_cut_after_its_prompt_keeps_its_own_partial_spend(spans):
    source = _Source()
    source.last_prompt_stats = _Stats(5.0)

    async def stream():
        source.last_prompt_stats = _Stats(0.25)  # this attempt's fresh stats
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial")
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _drain(source, stream())
    turn = _by_name(spans)["invoke_agent kirocrew-default"]
    assert turn.attributes["kirocrew.usage.credits"] == 0.25
    assert turn.attributes["kirocrew.usage.complete"] is False


@pytest.mark.asyncio
async def test_an_error_sets_status_and_class_name_never_the_message(spans):
    from opentelemetry.trace import StatusCode

    with pytest.raises(RuntimeError):
        await _drain(_Source(), _events(raise_exc=RuntimeError(f"{PROMPT} {SECRET}")))
    for name in ("invoke_agent kirocrew-default", "chat"):
        span = _by_name(spans)[name]
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["error.type"] == "RuntimeError"
        assert not span.events
    assert SECRET not in json.dumps([dict(s.attributes) for s in spans.get_finished_spans()])


@pytest.mark.asyncio
async def test_prompt_text_and_secrets_reach_no_attribute(spans):
    async def stream():
        tool_calls.note_tool_call_started("t1", kind="execute", scope="acp-1", tool_name="shell")
        yield AcpEvent(
            kind="tool_call",
            tool_call_id="t1",
            title=f"cat {SECRET}",
            tool_input=json.dumps({"command": f"echo {SECRET} /home/me/notes.txt"}),
        )
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"{PROMPT} {SECRET}")
        tool_calls.record_tool_call_finished("t1", status="completed", scope="acp-1")
        yield _complete(credits=1.0)

    await _drain(_Source(), stream())
    finished = spans.get_finished_spans()
    dumped = json.dumps([[s.name, dict(s.attributes)] for s in finished])
    for needle in (SECRET, PROMPT, "notes.txt", "/home/me"):
        assert needle not in dumped
    assert all(not s.events for s in finished)
