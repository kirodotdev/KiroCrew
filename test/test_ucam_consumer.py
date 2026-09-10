from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import ucam_consumer as consumer
from kiro_crew.acp.client import AcpClient


@pytest.fixture
def binding(tmp_path):
    return consumer.Binding(
        api_url="https://example.execute-api.us-east-1.amazonaws.com",
        scope="synthetic-scope",
        owner_sub="synthetic-owner",
        workspace="synthetic-workspace",
        generation="generation-proof",
        region="us-east-1",
        credentials_file=str(tmp_path / "credentials.json"),
        state_dir=str(tmp_path),
    )


def projection(binding, records=None):
    material = {
        "scope": binding.scope,
        "generation": binding.generation,
        "epoch": 1,
        "records": [] if records is None else records,
    }
    text = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {
        **material,
        "canonical_payload": text,
        "digest": consumer._sha(text),
        "valid_until": 1300,
    }


def record(binding):
    return {
        "scope": binding.scope,
        "status": "approved",
        "quarantined": False,
        "delivery": "standing",
        "revision": 1,
        "expires_at": 2000,
        "scope_context": {"user": binding.owner_sub, "workspace": binding.workspace},
        "synthetic": True,
        "exchange": {
            "id": "approved-test",
            "claim": "Synthetic preference: use cobalt labels.",
            "scope": "topic-only-not-authorization",
            "kind": "preference",
            "links": [],
            "lineage": {},
            "confidence": {"fraction": 1e-7, "large": 1e30, "negative_zero": -0.0},
        },
    }


class FakeAPI:
    def __init__(self, data, events=None):
        self.data = data
        self.events = [] if events is None else events
        self.fail_phase = None
        self.keys = []

    async def call(self, path, body=None, key=""):
        phase = body["phase"] if body else "projection"
        self.events.append(phase)
        self.keys.append(key)
        if self.fail_phase == phase:
            raise consumer.ConsumerError("ucam_api_unavailable")
        return (
            copy.deepcopy(self.data)
            if path == "/projection"
            else {
                "ack": {**body, "harness": "kirocrew", "scope": self.data["scope"]},
            }
        )


class FakeStdin:
    def __init__(self, events):
        self.events = events
        self.writes = []
        self.fail_drain = False

    def write(self, data):
        self.events.append("write")
        self.writes.append(json.loads(data))

    async def drain(self):
        self.events.append("drain")
        if self.fail_drain:
            raise BrokenPipeError()


class FakeTransport:
    _send_request = AcpClient._send_request

    def __init__(self, events):
        self._session_id = "fresh-synthetic-acp-session"
        self._process = SimpleNamespace(stdin=FakeStdin(events))

    def _next_req_id(self):
        return 1


class FakeProvider:
    def __init__(self, events):
        self._client = FakeTransport(events)

    async def stream(self, message):
        await self._client._send_request(
            "session/prompt",
            {
                "sessionId": self._client._session_id,
                "prompt": [{"type": "text", "text": message}],
            },
        )
        yield SimpleNamespace(kind="text_chunk", text="synthetic reply")


async def collect(stream):
    return [event async for event in stream]


def test_exact_fractional_canonical_payload(binding):
    data = projection(binding, [record(binding)])
    data["canonical_payload"] = data["canonical_payload"].replace("1e+30", "1.0e+30")
    data["digest"] = consumer._sha(data["canonical_payload"])
    material = consumer.verify_projection(data, binding, 1000)
    assert material["records"][0]["exchange"]["confidence"]["large"] == 1e30


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "wrong-scope"),
        ("generation", "wrong-generation"),
        ("epoch", True),
        ("epoch", 1.5),
        ("epoch", 9007199254740992),
        ("valid_until", 1000),
        ("valid_until", 1901),
        ("valid_until", True),
        ("records", {}),
    ],
)
def test_projection_rejects_bad_fields(binding, field, value):
    data = projection(binding)
    data[field] = value
    with pytest.raises(consumer.ConsumerError):
        consumer.verify_projection(data, binding, 1000)


@pytest.mark.parametrize("mutation", ["outer_claim", "duplicate", "missing_payload", "nonfinite"])
def test_projection_rejects_unverified_content(binding, mutation):
    data = projection(binding, [record(binding)])
    if mutation == "outer_claim":
        data["records"][0]["exchange"]["claim"] = "Unverified replacement"
    elif mutation == "duplicate":
        data["canonical_payload"] = data["canonical_payload"].replace(
            '"epoch":1', '"epoch":1,"epoch":1'
        )
        data["digest"] = consumer._sha(data["canonical_payload"])
    elif mutation == "missing_payload":
        del data["canonical_payload"]
    else:
        data["canonical_payload"] = data["canonical_payload"].replace('"epoch":1', '"epoch":NaN')
        data["digest"] = consumer._sha(data["canonical_payload"])
    with pytest.raises(consumer.ConsumerError):
        consumer.verify_projection(data, binding, 1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "candidate"),
        ("quarantined", True),
        ("delivery", "retrieval"),
        ("scope", "other"),
        ("expires_at", 1299),
        ("revalidate_at", 1299),
        ("revision", True),
        ("scope_context", {"user": "other", "workspace": "synthetic-workspace"}),
        ("scope_context", {"user": "synthetic-owner", "workspace": "other"}),
    ],
)
def test_signed_candidate_or_expired_record_not_injected(binding, field, value):
    entry = record(binding)
    entry[field] = value
    with pytest.raises(consumer.ConsumerError):
        consumer.verify_projection(projection(binding, [entry]), binding, 1000)


@pytest.mark.asyncio
async def test_real_send_method_ack_only_after_drain(binding):
    events = []
    api = FakeAPI(projection(binding, [record(binding)]), events)
    run = consumer.ConsumerRun(binding, "run-1", api, clock=lambda: 1000)
    provider = FakeProvider(events)
    assert len(await collect(run.stream(provider, "Synthetic task"))) == 1
    assert events == ["projection", "fetched", "write", "drain", "injected", "turn_result"]
    prompt = provider._client._process.stdin.writes[0]["params"]["prompt"]
    assert prompt[0]["text"] == "Synthetic task"
    assert "cobalt labels" in prompt[1]["text"]
    assert run.sent and consumer._active_run.get() is None
    assert all(consumer.HASH.fullmatch(key) for key in api.keys[1:])


@pytest.mark.asyncio
async def test_unrelated_transport_unchanged(binding):
    provider = FakeProvider([])
    await collect(provider.stream("Unchanged legacy message"))
    assert provider._client._process.stdin.writes[0]["params"]["prompt"] == [
        {"type": "text", "text": "Unchanged legacy message"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["projection", "fetched"])
async def test_outage_no_send_no_injected_ack(binding, phase):
    events = []
    api = FakeAPI(projection(binding), events)
    api.fail_phase = phase
    run = consumer.ConsumerRun(binding, "run-outage", api, clock=lambda: 1000)
    provider = FakeProvider(events)
    with pytest.raises(consumer.ConsumerError):
        await collect(run.stream(provider, "Task"))
    assert not provider._client._process.stdin.writes
    assert "injected" not in events and consumer._active_run.get() is None


@pytest.mark.asyncio
async def test_lease_expiry_during_fetched_ack_no_write(binding):
    now = [1000]
    api = FakeAPI(projection(binding))
    original = api.call

    async def call(path, body=None, key=""):
        response = await original(path, body, key)
        if body and body["phase"] == "fetched":
            now[0] = 1300
        return response

    api.call = call
    provider = FakeProvider([])
    run = consumer.ConsumerRun(binding, "expired-after-queue", api, clock=lambda: now[0])
    with pytest.raises(consumer.ConsumerError, match="ucam_lease"):
        await collect(run.stream(provider, "Task"))
    assert not provider._client._process.stdin.writes


@pytest.mark.asyncio
async def test_failed_drain_never_acknowledged_or_retried(binding):
    events = []
    run = consumer.ConsumerRun(
        binding, "drain-failed", FakeAPI(projection(binding), events), clock=lambda: 1000
    )
    provider = FakeProvider(events)
    provider._client._process.stdin.fail_drain = True
    with pytest.raises(Exception):
        await collect(run.stream(provider, "Task"))
    assert "injected" not in events and not run.sent
    with pytest.raises(consumer.ConsumerError, match="ucam_fresh_session_required"):
        await collect(run.stream(provider, "Task retry"))


@pytest.mark.asyncio
async def test_restart_run_replay_rejected(binding):
    provider = FakeProvider([])
    await collect(
        consumer.ConsumerRun(
            binding,
            "same-run",
            FakeAPI(projection(binding)),
            clock=lambda: 1000,
        ).stream(provider, "Task")
    )
    with pytest.raises(consumer.ConsumerError, match="ucam_run_already_attempted"):
        await collect(
            consumer.ConsumerRun(
                binding,
                "same-run",
                FakeAPI(projection(binding)),
                clock=lambda: 1000,
            ).stream(provider, "Task")
        )
    assert len(provider._client._process.stdin.writes) == 1


@pytest.mark.asyncio
async def test_post_send_ack_outage_does_not_resend(binding):
    events = []
    api = FakeAPI(projection(binding), events)
    api.fail_phase = "injected"
    provider = FakeProvider(events)
    run = consumer.ConsumerRun(binding, "ack-outage", api, clock=lambda: 1000)
    await collect(run.stream(provider, "Task"))
    assert run.sent and run.ack_failed and events.count("write") == 1
    assert "turn_result" in events


@pytest.mark.asyncio
async def test_parallel_unrelated_session_not_injected(binding):
    synthetic = FakeProvider([])
    unrelated = FakeProvider([])
    run = consumer.ConsumerRun(
        binding, "parallel", FakeAPI(projection(binding)), clock=lambda: 1000
    )
    await asyncio.gather(
        collect(run.stream(synthetic, "Synthetic")), collect(unrelated.stream("Ordinary"))
    )
    assert len(synthetic._client._process.stdin.writes[0]["params"]["prompt"]) == 2
    assert len(unrelated._client._process.stdin.writes[0]["params"]["prompt"]) == 1


@pytest.mark.asyncio
async def test_other_app_resume_does_not_read_config(monkeypatch):
    loader = AsyncMock(side_effect=AssertionError("No config read for unrelated app"))
    monkeypatch.setattr(consumer, "load_binding", loader)
    assert await consumer.consumer_for(SimpleNamespace(app="other"), False, True, False) is None
    loader.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("agent", "other"),
        ("keep", True),
        ("conversation_key", "old-session"),
        ("_cancel_retry_used", True),
    ],
)
async def test_bound_identity_and_resume_refused(field, value):
    attributes = {"app": consumer.APP_NAME, "agent": consumer.AGENT_NAME, "id": "run"}
    attributes[field] = value
    with pytest.raises(consumer.ConsumerError):
        await consumer.consumer_for(SimpleNamespace(**attributes), True, False, False)


@pytest.fixture
def routes():
    path = Path(__file__).parents[1] / "addons/ucam-synthetic-consumer/backend/routes.py"
    spec = importlib.util.spec_from_file_location("ucam_test_routes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Request(dict):
    content_length = 100

    def __init__(self, app=consumer.APP_NAME, user=consumer.APP_NAME, body=None):
        super().__init__(app=app, user=user)
        self.body = {"task": "Synthetic task"} if body is None else body

    async def json(self):
        return self.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "app,user,body,status",
    [
        (consumer.APP_NAME, consumer.APP_NAME, {"task": "Task", "request_id": "request-1"}, 202),
        ("owui-bridge", consumer.APP_NAME, {"task": "Task"}, 403),
        (consumer.APP_NAME, "other-owner", {"task": "Task"}, 403),
        ("", consumer.APP_NAME, {"task": "Task"}, 403),
        (consumer.APP_NAME, consumer.APP_NAME, {"task": "Task", "workspace": "other"}, 400),
        (consumer.APP_NAME, consumer.APP_NAME, {"task": "Task", "agent": "kirocrew"}, 400),
        (consumer.APP_NAME, consumer.APP_NAME, {"task": "Task", "owner_sub": "other"}, 400),
    ],
)
async def test_route_verified_identity_only(binding, routes, monkeypatch, app, user, body, status):
    monkeypatch.setattr(routes, "load_binding", AsyncMock(return_value=binding))
    spawn = AsyncMock(return_value="fresh-run-id")
    ctx = SimpleNamespace(name=consumer.APP_NAME, spawn=SimpleNamespace(run=spawn))
    response = await routes.consume(Request(app, user, body), ctx)
    assert response.status == status
    payload = json.loads(response.body)
    if status == 202:
        spawn.assert_awaited_once_with("Task", agent=consumer.AGENT_NAME, silent=True)
        assert payload == {"run_id": "fresh-run-id", "phase": "queued"}
    else:
        assert payload["code"]
        spawn.assert_not_awaited()


def test_config_requires_explicit_temporary_credentials_file(binding, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**asdict(binding), "enabled": True}))
    assert consumer.Binding.read(config) == binding
    config.write_text(json.dumps({**asdict(binding), "enabled": False}))
    with pytest.raises(consumer.ConsumerError, match="ucam_disabled"):
        consumer.Binding.read(config)


@pytest.mark.asyncio
async def test_final_write_lease_check_after_serialization(binding):
    now = [1000]
    provider = FakeProvider([])

    def delayed_next_id():
        now[0] = 1300
        return 1

    provider._client._next_req_id = delayed_next_id
    run = consumer.ConsumerRun(
        binding, "write-expired", FakeAPI(projection(binding)), clock=lambda: now[0]
    )
    with pytest.raises(consumer.ConsumerError, match="ucam_lease"):
        await collect(run.stream(provider, "Task"))
    assert not provider._client._process.stdin.writes


@pytest.mark.asyncio
async def test_wrong_grant_harness_refused_before_write(binding):
    api = FakeAPI(projection(binding))
    original = api.call

    async def wrong_harness(path, body=None, key=""):
        response = await original(path, body, key)
        if body:
            response["ack"]["harness"] = "codex"
        return response

    api.call = wrong_harness
    provider = FakeProvider([])
    run = consumer.ConsumerRun(binding, "wrong-harness", api, clock=lambda: 1000)
    with pytest.raises(consumer.ConsumerError, match="ucam_ack_binding"):
        await collect(run.stream(provider, "Task"))
    assert not provider._client._process.stdin.writes


@pytest.mark.asyncio
async def test_generator_close_has_no_context_leak(binding):
    run = consumer.ConsumerRun(binding, "close", FakeAPI(projection(binding)), clock=lambda: 1000)
    iterator = run.stream(FakeProvider([]), "Task")
    await iterator.__anext__()
    assert consumer._active_run.get() is None
    await asyncio.create_task(iterator.aclose())
    assert consumer._active_run.get() is None


@pytest.mark.asyncio
async def test_signed_api_timeout_is_bounded(binding, monkeypatch):
    import aiohttp

    Path(binding.credentials_file).write_text(
        json.dumps(
            {
                "access_key": "synthetic-not-real",
                "secret_key": "synthetic-not-real",
                "token": "synthetic-not-real",
                "expires_at": 9999999999,
            }
        )
    )

    class HangingSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(aiohttp, "ClientSession", HangingSession)
    monkeypatch.setattr(consumer, "IO_TIMEOUT", 0.02)
    with pytest.raises(consumer.ConsumerError, match="ucam_api_unavailable"):
        await asyncio.wait_for(consumer.SignedAPI(binding).call("/projection"), 0.5)
