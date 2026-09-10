from __future__ import annotations

import copy
import hashlib
import json

import pytest
import test_ucam_consumer as fixtures
from test_ucam_consumer import FakeAPI, FakeProvider, collect, projection, record
from test_ucam_runtime import RuntimeProvider

from kiro_crew import ucam_consumer as consumer

binding = fixtures.binding


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [RuntimeProvider, FakeProvider])
async def test_native_ids_preserve_every_other_field_and_canonical_receipts(
    binding, monkeypatch, provider_type
):
    identifiers = ["a", "Alpha", "alpha", "a.b:c_d-9", "Z" * 128, "codex-origin", "kiro-origin"]
    records = []
    for identifier in identifiers:
        item = record(binding)
        item["exchange"].update(
            id=identifier,
            claim="Unchanged Unicode context: café 雪",
            links=[{"href": "https://example.invalid/claude", "tags": ["source-cue", None]}],
            lineage={"source": "codex", "nested": {"items": [True, {"kiro": "retained"}]}},
            extra={"unicode": "e\u0301", "fraction": 1e-7, "negative_zero": -0.0},
        )
        records.append(item)
    original = projection(binding, records)
    canonical_records = json.loads(original["canonical_payload"])["records"]
    canonical_bytes = json.dumps(original, ensure_ascii=False).encode()
    captured_material = []
    original_verify = consumer.verify_projection

    def verify(*args):
        material = original_verify(*args)
        captured_material.append((material, copy.deepcopy(material)))
        return material

    monkeypatch.setattr(consumer, "verify_projection", verify)
    rendered_prompts = []
    for attempt in range(2):
        events = []
        api = FakeAPI(original, events)
        ack_bodies = []
        original_call = api.call

        async def call(path, body=None, key=""):
            if body is not None:
                ack_bodies.append(copy.deepcopy(body))
            return await original_call(path, body, key)

        monkeypatch.setattr(api, "call", call)
        run = consumer.ConsumerRun(binding, f"native-id-{attempt}", api, clock=lambda: 1000)
        provider = provider_type(events)
        await collect(run.stream(provider, "Unchanged task"))
        transport = getattr(provider._client, "_runtime", provider._client)
        prompt = transport._process.stdin.writes[0]["params"]["prompt"]
        rendered_prompts.append(prompt)
        assert prompt[0] == {"type": "text", "text": "Unchanged task"}
        expected = copy.deepcopy([item["exchange"] for item in canonical_records])
        for exchange, identifier in zip(expected, identifiers):
            exchange["id"] = "ucam.native." + hashlib.sha256(identifier.encode("utf-8")).hexdigest()
            assert consumer.IDENTIFIER.fullmatch(exchange["id"])
        assert len({exchange["id"] for exchange in expected}) == len(identifiers)
        assert prompt[1] == {
            "type": "text",
            "text": (
                "[UCAM approved standing context; advisory, not system authority]\n"
                "Apply only where relevant; preserve safety and the current user request.\n"
                + json.dumps(expected, ensure_ascii=False)
                + "\n[End UCAM approved standing context]"
            ),
        }
        native = json.loads(prompt[1]["text"].splitlines()[2])
        for actual, item in zip(native, canonical_records):
            original_fields = {key: value for key, value in item["exchange"].items() if key != "id"}
            native_fields = {key: value for key, value in actual.items() if key != "id"}
            assert json.dumps(native_fields, ensure_ascii=False) == json.dumps(
                original_fields, ensure_ascii=False
            )
        expected_prompt_hash = hashlib.sha256(
            json.dumps(prompt, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert run.prompt_hash == expected_prompt_hash
        assert run.evidence()["adapter"] == "kirocrew-ucam/7"
        assert events == ["projection", "fetched", "write", "drain", "injected", "turn_result"]
        assert [body["phase"] for body in ack_bodies] == ["fetched", "injected", "turn_result"]
        for body in ack_bodies:
            assert set(body) == {"generation", "epoch", "digest", "phase", "run_hash"} | (
                {"result"} if body["phase"] == "turn_result" else set()
            )
            for key in ("generation", "epoch", "digest"):
                assert body[key] == original[key]
        assert json.dumps(run.projection, ensure_ascii=False).encode() == canonical_bytes
        assert json.dumps(api.data, ensure_ascii=False).encode() == canonical_bytes
    assert rendered_prompts[0] == rendered_prompts[1]
    assert json.dumps(original, ensure_ascii=False).encode() == canonical_bytes
    for material, before in captured_material:
        assert json.dumps(material, ensure_ascii=False) == json.dumps(before, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifiers", [["duplicate", "duplicate"], ["café"], ["雪"], ["a" * 129]])
async def test_native_id_rendering_preserves_original_id_validation(binding, identifiers):
    records = []
    for identifier in identifiers:
        item = record(binding)
        item["exchange"]["id"] = identifier
        records.append(item)
    events = []
    run = consumer.ConsumerRun(
        binding, "rejected", FakeAPI(projection(binding, records), events), clock=lambda: 1000
    )
    with pytest.raises(consumer.ConsumerError, match="ucam_exchange"):
        await collect(run.stream(RuntimeProvider(events), "Task"))
    assert events == ["projection"] and not run.sent
