import asyncio
import json
import logging
import re

import pytest
import test_ucam_consumer as fixtures
from test_ucam_consumer import FakeAPI, FakeProvider, collect, projection, record
from test_ucam_runtime import RuntimeProvider

from kiro_crew import ucam_consumer as consumer
from kiro_crew.acp.runtime import AcpRuntimeDead

binding = fixtures.binding


@pytest.fixture
def warning_logs(caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(consumer.logger, "level", logging.NOTSET)
    monkeypatch.setattr(consumer.logger.parent, "level", logging.WARNING)
    assert not consumer.logger.isEnabledFor(logging.INFO)
    assert consumer.logger.isEnabledFor(logging.WARNING)
    return caplog


def native_logs(caplog):
    return [entry for entry in caplog.records if entry.getMessage().startswith("UCAM native_write")]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [RuntimeProvider, FakeProvider])
@pytest.mark.parametrize("fail_phase", [None, "injected", "turn_result"])
async def test_native_observer_hash_only_after_drain(
    binding, warning_logs, monkeypatch, provider_type, fail_phase
):
    events = []
    approved = record(binding)
    approved["exchange"]["claim"] = "PRIVATE-CONTEXT\nBearer SYNTHETIC-CREDENTIAL"
    api = FakeAPI(projection(binding, [approved]), events)
    api.fail_phase = fail_phase
    run = consumer.ConsumerRun(binding, "PRIVATE-RUN-ID", api, clock=lambda: 1000)
    provider = provider_type(events)
    transport = getattr(provider._client, "_runtime", provider._client)
    stdin = transport._process.stdin
    original_drain = stdin.drain
    original_call = api.call
    levels = (consumer.logger.level, consumer.logger.parent.level, logging.root.level)
    handlers = tuple(logging.root.handlers)

    async def drain():
        assert not native_logs(warning_logs)
        await original_drain()
        assert not native_logs(warning_logs)

    async def call(path, body=None, key=""):
        if body and body["phase"] == "injected":
            assert events[-1] == "drain"
            assert len(native_logs(warning_logs)) == 1
        return await original_call(path, body, key)

    monkeypatch.setattr(stdin, "drain", drain)
    monkeypatch.setattr(api, "call", call)
    consumer.logger.info("PRIVATE-INFO-BEFORE")
    logging.getLogger("kiro_crew.acp.runtime").info("PRIVATE-TRANSPORT-INFO")
    await collect(run.stream(provider, "PRIVATE-TASK\nAuthorization: SYNTHETIC-CREDENTIAL"))
    consumer.logger.info("PRIVATE-INFO-AFTER")

    assert events == ["projection", "fetched", "write", "drain", "injected", "turn_result"]
    assert len(stdin.writes) == 1
    outgoing = stdin.writes[0]["params"]["prompt"]
    expected_prompt_hash = consumer._sha(json.dumps(outgoing, ensure_ascii=False, sort_keys=True))
    entries = native_logs(warning_logs)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == consumer.logger.name and entry.levelno == logging.WARNING
    assert entry.exc_info is None and entry.stack_info is None
    assert entry.getMessage() == (
        f"UCAM native_write adapter={consumer.ADAPTER_VERSION} run_hash={run.run_hash} "
        f"digest={api.data['digest']} prompt_hash={expected_prompt_hash}"
    )
    assert re.fullmatch(
        r"UCAM native_write adapter=kirocrew-ucam/7 run_hash=[a-f0-9]{64} "
        r"digest=[a-f0-9]{64} prompt_hash=[a-f0-9]{64}",
        entry.getMessage(),
    )
    assert run.sent and run.ack_failed is (fail_phase is not None)
    assert "PRIVATE" not in warning_logs.text and "SYNTHETIC-CREDENTIAL" not in warning_logs.text
    for private_value in (binding.owner_sub, binding.workspace, binding.credentials_file):
        assert private_value not in warning_logs.text
    assert "UCAM ack phase=" not in warning_logs.text
    assert not consumer.logger.isEnabledFor(logging.INFO)
    assert levels == (consumer.logger.level, consumer.logger.parent.level, logging.root.level)
    assert handlers == tuple(logging.root.handlers)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["projection", "fetched", "write", "drain", "cancel"])
async def test_runtime_no_native_observer_before_successful_drain(
    binding, warning_logs, monkeypatch, failure
):
    events = []
    api = FakeAPI(projection(binding), events)
    if failure in ("projection", "fetched"):
        api.fail_phase = failure
    run = consumer.ConsumerRun(binding, "run-failed", api, clock=lambda: 1000)
    provider = RuntimeProvider(events)
    runtime = provider._client._runtime
    runtime._pid = None
    runtime._stderr_lines = []
    runtime._pending_requests = {}
    runtime._pending_init_notifications = {}
    runtime._process.returncode = None

    def write(data):
        raise BrokenPipeError()

    async def cancel():
        raise asyncio.CancelledError()

    if failure == "write":
        monkeypatch.setattr(runtime._process.stdin, "write", write)
    elif failure == "drain":
        runtime._process.stdin.fail_drain = True
    elif failure == "cancel":
        monkeypatch.setattr(runtime._process.stdin, "drain", cancel)
    expected = (
        consumer.ConsumerError
        if failure in ("projection", "fetched")
        else asyncio.CancelledError if failure == "cancel" else AcpRuntimeDead
    )
    with pytest.raises(expected):
        await collect(run.stream(provider, "Task"))
    assert not native_logs(warning_logs)
    assert not run.sent and "injected" not in events and "turn_result" not in events


@pytest.mark.asyncio
async def test_runtime_expired_lease_after_serialization_has_no_observer(
    binding, warning_logs, monkeypatch
):
    from kiro_crew.acp import runtime as runtime_module

    events = []
    now = [1000]
    api = FakeAPI(projection(binding), events)
    run = consumer.ConsumerRun(binding, "expired-run", api, clock=lambda: now[0])
    provider = RuntimeProvider(events)
    original_dumps = json.dumps

    def delayed_dumps(value, *args, **kwargs):
        encoded = original_dumps(value, *args, **kwargs)
        if isinstance(value, dict) and value.get("method") == "session/prompt":
            now[0] = 1300
        return encoded

    monkeypatch.setattr(runtime_module.json, "dumps", delayed_dumps)
    with pytest.raises(consumer.ConsumerError, match="ucam_lease"):
        await collect(run.stream(provider, "Task"))
    assert events == ["projection", "fetched"]
    assert not native_logs(warning_logs) and not run.sent


@pytest.mark.asyncio
async def test_unrelated_runtime_and_non_prompt_have_no_native_observer(binding, warning_logs):
    events = []
    provider = RuntimeProvider(events)
    await collect(provider.stream("Unchanged"))
    run = consumer.ConsumerRun(binding, "unused-run", FakeAPI(projection(binding), events))
    token = consumer._active_run.set(run)
    try:
        await provider._client._runtime.send_request("session/cancel", {"sessionId": "unrelated"})
    finally:
        consumer._active_run.reset(token)
    assert events == ["write", "drain", "write", "drain"]
    assert not native_logs(warning_logs) and not run.used
