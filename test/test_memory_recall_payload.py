"""The actual HTTP/MCP JSON contains selected snippets, never full source rows."""

import json

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import embeddings, mcp_shared
from kiro_crew.dashboard.handlers import memory_member
from kiro_crew.mcp_tools import learn
from kiro_crew.memory_recall import (
    MAX_RECALL_PAYLOAD_BYTES,
    _transport_size,
    bound_recall_payload,
    recall_json,
    v2_operating_point,
)
from kiro_crew.validation import build_tool_response

env = _member_env


def test_large_cjk_http_payload_uses_utf8_budget_without_shortening():
    text = "数据库备份" * 300
    payload = {
        "semantic_context": "",
        "episodic_context": f"[memory:episode-cjk] {text}\n[memory:episode-tail] tail\n",
        "lessons_context": "",
        "retrieval": {
            "facts": [],
            "episodes": [
                {"id": "episode-cjk", "text": text},
                {"id": "episode-tail", "text": "tail"},
            ],
        },
    }

    bounded = bound_recall_payload(payload)

    assert [row["id"] for row in bounded["retrieval"]["episodes"]] == [
        "episode-cjk",
        "episode-tail",
    ]
    assert bounded["retrieval"]["episodes"][0]["text"] == text
    assert "text_truncated" not in bounded["retrieval"]["episodes"][0]
    assert "episode-cjk" in bounded["episodic_context"]
    wire = recall_json(bounded, ensure_ascii=False)
    assert text in wire
    assert len(wire.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES


def test_large_cjk_mcp_payload_marks_actual_outer_envelope_truncation():
    text = "数据库备份" * 300
    payload = {
        "semantic_context": "",
        "episodic_context": f"[memory:episode-cjk] {text}\n",
        "lessons_context": "",
        "retrieval": {
            "facts": [],
            "episodes": [{"id": "episode-cjk", "text": text}],
        },
    }

    wire = recall_json(payload, ensure_ascii=False, mcp_envelope=True)
    selected = json.loads(wire)

    assert selected["retrieval"]["episodes"][0]["text_truncated"] is True
    assert "[truncated]" in selected["episodic_context"]
    assert _transport_size(wire, mcp_envelope=True) <= MAX_RECALL_PAYLOAD_BYTES


@pytest.mark.parametrize(
    "bundled,registered,model_id,dim,qualification",
    [
        (True, False, "qwen3-embedding:0.6b", 1024, "reference_identity"),
        (False, False, "qwen3-embedding:0.6b", 1024, "custom_or_registered"),
        (True, True, "qwen3-embedding:0.6b", 1024, "custom_or_registered"),
        (False, False, "custom-space", 768, "custom_or_registered"),
        (True, False, "qwen3-embedding:0.6b", 768, "custom_or_registered"),
    ],
)
def test_v2_empty_recall_qualifies_declared_identity_without_loading(
    env, monkeypatch, bundled, registered, model_id, dim, qualification
):
    # Identity-only object: no constructor, weights, readiness state or inference
    # methods are prepared. The diagnostic must only read the declared identity.
    backend = object.__new__(embeddings.LlamaCppEmbedder)
    backend._model_id = model_id
    backend._dim = dim
    backend._uses_bundled_identity = bundled

    def unexpected_construction():
        pytest.fail("recall diagnostics must not construct a model")

    monkeypatch.setattr(embeddings, "_shared_embedder", backend)
    monkeypatch.setattr(
        embeddings, "_backend_factory", unexpected_construction if registered else None
    )
    monkeypatch.setattr(embeddings, "get_shared_embedder", unexpected_construction)
    tier = env.tiers["member-alice"]
    tier.embed_fn = embeddings.make_sync_embed_fn()
    signature = embeddings.embedding_space_signature(model_id, dim)
    tier._write_meta("embedding_space_sig", signature)

    result = tier.recall("", cap=0)

    point = result["retrieval"]["operating_point"]
    assert point == {
        "status": "provisional",
        "qualification": qualification,
        "short_cosine_floor": 0.62,
        "long_cosine_floor": 0.57,
        "long_text_chars": 300,
        "reference_signature": embeddings.embedding_space_signature("qwen3-embedding:0.6b", 1024),
        "active_signature": signature,
        "stored_signature": signature,
        "stored_matches_active": True,
        "signature_basis": "declared_model_id_and_dimension",
    }
    assert result["total_chars"] == 0
    assert result["retrieval"]["facts"] == result["retrieval"]["episodes"] == []
    assert json.loads(recall_json(result))["retrieval"]["operating_point"] == point
    assert "operating_point" not in env.tiers[""].recall("", cap=0)["retrieval"]


@pytest.mark.parametrize("registered", [False, True])
def test_v2_uninitialized_backend_never_constructs_for_a_diagnostic(env, monkeypatch, registered):
    def unexpected_construction():
        pytest.fail("recall diagnostics must not construct a model")

    monkeypatch.setattr(embeddings, "_shared_embedder", None)
    monkeypatch.setattr(
        embeddings, "_backend_factory", unexpected_construction if registered else None
    )
    monkeypatch.setattr(embeddings, "get_shared_embedder", unexpected_construction)
    result = env.tiers["member-alice"].recall("", cap=0)
    point = result["retrieval"]["operating_point"]
    assert point["qualification"] == ("custom_or_registered" if registered else "unknown")
    assert point["active_signature"] is None
    assert point["stored_signature"] is None
    assert point["stored_matches_active"] is None
    assert len(recall_json(result).encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES


def test_v2_custom_callable_does_not_inherit_the_shared_backends_identity(monkeypatch):
    monkeypatch.setattr(
        embeddings,
        "peek_shared_embedding_identity",
        lambda: (embeddings.default_embedding_space_signature(), "bundled"),
    )
    point = v2_operating_point("a" * 16, embed_fn=lambda text: [1.0])
    assert point["qualification"] == "custom_or_registered"
    assert point["active_signature"] is None
    assert point["stored_matches_active"] is None


def test_v2_different_or_invalid_recorded_identity_is_explicit_and_bounded(monkeypatch):
    monkeypatch.setattr(
        embeddings,
        "peek_shared_embedding_identity",
        lambda: (embeddings.default_embedding_space_signature(), "bundled"),
    )
    point = v2_operating_point("b" * 16)
    assert point["stored_matches_active"] is False
    assert point["qualification"] == "reference_identity"
    point = v2_operating_point("untrusted metadata" * 10000)
    assert point["stored_signature"] is None
    assert point["stored_matches_active"] is None
    assert len(json.dumps(point)) < 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
@pytest.mark.parametrize("legacy_oversized", [False, True])
async def test_long_episode_is_a_snippet_in_actual_http_and_mcp_json(
    env, monkeypatch, name, legacy_oversized
):
    tier = env.tiers[name]
    source = "PostgreSQL database decision. " + "Historical detail. " * 90 + "PRIVATE_TAIL_SENTINEL"
    assert tier.write_episodic(source, importance=1.0, defer_embedding=True)
    original = tier.get_episodic_list()[0]
    if legacy_oversized:
        # Older/restored SQLite rows can predate today's write-time 2000-char
        # admission cap. Retrieval must still project only its selected snippet.
        source = (
            "PostgreSQL database decision. "
            + "Historical detail. " * 2000
            + "PRIVATE_TAIL_SENTINEL"
        )
        tier.db.execute(f"UPDATE {tier._epi_rel} SET text=? WHERE id=?", (source, original["id"]))
        tier.db.commit()
        tier._invalidate_episodic_scoring()
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )
    assert response.status == 200
    payload = json.loads(response.text)
    evidence = payload["retrieval"]["episodes"]
    assert [row["id"] for row in evidence] == [original["id"]]
    assert evidence[0]["text"] == source[:1500]
    assert evidence[0]["text_truncated"] is True
    assert evidence[0]["text"] in payload["episodic_context"]
    assert "[truncated]" in payload["episodic_context"]
    assert "PRIVATE_TAIL_SENTINEL" not in response.text
    assert len(response.body) <= MAX_RECALL_PAYLOAD_BYTES
    assert payload["total_chars"] <= 3000
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key_strict", lambda: "dashboard:alice")
    monkeypatch.setattr(learn.mcp_core, "_get", lambda *args, **kwargs: payload)
    result = learn.memory_recall("memory_recall", {"query": "PostgreSQL database"})
    assert len(result.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES
    assert "PRIVATE_TAIL_SENTINEL" not in result
    assert json.loads(result)["retrieval"]["episodes"] == evidence
    assert tier.get_episodic_list()[0]["text"] == source


@pytest.mark.parametrize("name", ["", "member-alice"])
def test_unicode_and_metadata_overhead_fit_whole_payload_with_matching_ids(env, name):
    tier = env.tiers[name]
    for index in range(18):
        assert (
            tier.set_semantic(
                f"project.database_{index}",
                "PostgreSQL database " + "🚀数据库" * 30,
                1.0,
                "user_explicit",
            )
            is None
        )
    result = tier.recall("PostgreSQL database", cap=12000)
    wire = recall_json(result)
    assert len(wire.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES
    unicode_wire = recall_json(result, ensure_ascii=False)
    assert "数据库" in unicode_wire
    assert len(unicode_wire.encode("utf-8")) <= MAX_RECALL_PAYLOAD_BYTES
    assert "error" not in result
    assert result["total_chars"] <= 12000
    assert result["retrieval"]["facts"]
    assert result["retrieval"].get("omitted_for_payload_budget", 0) > 0
    if name:
        assert result["retrieval"]["operating_point"]["status"] == "provisional"
        assert (
            json.loads(wire)["retrieval"]["operating_point"]
            == result["retrieval"]["operating_point"]
        )
    else:
        assert "operating_point" not in result["retrieval"]
    for row in result["retrieval"]["facts"]:
        assert f"[memory:{row['id']}] {row['snippet']}" in result["semantic_context"]
        assert "embedding" not in row and "value_json" not in row
    assert len(tier.get_all_semantic()) == 18


@pytest.mark.parametrize("name", ["", "member-alice"])
def test_selected_evidence_keeps_source_identity_and_compact_copy_provenance(
    env, monkeypatch, name
):
    tier = env.tiers[name]
    monkeypatch.setattr(
        tier,
        "_semantic_candidates_v2" if name else "_semantic_candidates_v1",
        lambda query: [
            {
                "key": "project.database",
                "value_json": '"PostgreSQL"',
                "source": "user_seed",
                "embedding": b"not transport data",
                "derived_from": json.dumps(
                    {"kind": "fact", "store": "member-bob", "item_id": "original-key"}
                ),
                "retrieval": {"reason": "keyword_match", "matched_terms": ["database"]},
                "unused_source_body": "DO_NOT_RETURN" * 10000,
            }
        ],
    )
    result = tier.recall("database")
    row = result["retrieval"]["facts"][0]
    assert row["id"] == "key:project.database"
    assert row["source"] == "user_seed"
    assert row["derived_from"] == {"kind": "fact", "store": "member-bob", "item_id": "original-key"}
    assert row["retrieval"]["matched_terms"] == ["database"]
    assert "DO_NOT_RETURN" not in recall_json(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
@pytest.mark.parametrize("content_length", [False, True])
async def test_actual_stdio_frame_counts_nested_json_escaping_and_content_wrapper(
    env, monkeypatch, name, content_length
):
    tier = env.tiers[name]
    for index in range(18):
        tier.set_semantic(
            f"project.database_{index}",
            "PostgreSQL database " + '🚀数据库\\"\n' * 25,
            1.0,
            "user_explicit",
        )
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )
    assert response.status == 200
    assert len(response.body) <= MAX_RECALL_PAYLOAD_BYTES
    payload = json.loads(response.text)
    assert payload["retrieval"]["facts"]
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key_strict", lambda: "dashboard:alice")
    monkeypatch.setattr(learn.mcp_core, "_resolve_session_key", lambda: "dashboard:alice")
    monkeypatch.setattr(learn.mcp_core, "_get", lambda *args, **kwargs: payload)
    text = learn.mcp_core._call_tool("memory_recall", {"query": "PostgreSQL database"})
    # Capture the production writer's bytes, after its default ensure_ascii=True
    # JSON encoder. A UTF-8 count of the inner handler string misses this layer.
    frames = []
    monkeypatch.setattr(mcp_shared, "_use_content_length", content_length)
    monkeypatch.setattr(mcp_shared, "_stdout_fd", 98765)
    monkeypatch.setattr(mcp_shared, "_write_all", lambda fd, frame: frames.append(frame))
    request_id = "memory-recall-76c7803c-9890-4427-8500-51c7b531fc99"
    mcp_shared.respond(request_id, build_tool_response(text))
    assert len(frames) == 1
    frame = frames[0]
    assert len(frame) <= MAX_RECALL_PAYLOAD_BYTES
    if content_length:
        header, body = frame.split(b"\r\n\r\n", 1)
        assert int(header.removeprefix(b"Content-Length: ")) == len(body)
    else:
        assert frame.endswith(b"\n")
        body = frame
    assert b"\\u6570" in body
    envelope = json.loads(body)
    assert envelope["id"] == request_id
    assert envelope["result"] == {"content": [{"type": "text", "text": text}]}
    selected = json.loads(envelope["result"]["content"][0]["text"])
    assert selected["total_chars"] <= 3000
    assert {row["id"] for row in selected["retrieval"]["facts"]}.issubset(
        {row["id"] for row in payload["retrieval"]["facts"]}
    )
    for row in selected["retrieval"]["facts"]:
        assert f"[memory:{row['id']}] {row['snippet']}" in selected["semantic_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
async def test_final_redaction_cannot_expand_context_past_requested_char_budget(
    env, monkeypatch, name
):
    tier = env.tiers[name]
    for index in range(12):
        tier.set_semantic(
            f"project.database_{index}", "PostgreSQL database TOKEN" * 5, 1.0, "user_explicit"
        )

    def expand_redaction(value):
        if isinstance(value, str):
            return value.replace("TOKEN", "[REDACTED CREDENTIAL]" * 10)
        if isinstance(value, dict):
            return {key: expand_redaction(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand_redaction(item) for item in value]
        return value

    monkeypatch.setattr(memory_member, "_redact_memory_field", expand_redaction)
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )
    payload = json.loads(response.text)
    assert payload["retrieval"]["facts"]
    assert "TOKEN" not in response.text
    assert any(row.get("snippet_truncated") for row in payload["retrieval"]["facts"])
    assert payload["total_chars"] == sum(
        len(payload[f"{kind}_context"]) for kind in ("semantic", "episodic", "lessons")
    )
    assert payload["total_chars"] <= 3000
    assert len(response.body) <= MAX_RECALL_PAYLOAD_BYTES
    for row in payload["retrieval"]["facts"]:
        assert f"[memory:{row['id']}] {row['snippet']}" in payload["semantic_context"]


@pytest.mark.parametrize("content_length", [False, True])
@pytest.mark.parametrize("kind,field", [("facts", "snippet"), ("episodes", "text")])
@pytest.mark.parametrize("context_cap", [32, 200])
def test_mcp_budget_covers_unicode_expansion_by_final_sanitizer(
    monkeypatch, content_length, kind, field, context_cap
):
    text = "database " + "\u0344" * 1500
    retrieval = {"facts": [], "episodes": []}
    retrieval[kind] = [{"id": "combining", field: text}]
    encoded = recall_json(
        {
            "semantic_context": "",
            "episodic_context": "",
            "lessons_context": "",
            "retrieval": retrieval,
        },
        ensure_ascii=False,
        mcp_envelope=True,
        context_cap=context_cap,
    )
    frames = []
    monkeypatch.setattr(mcp_shared, "_use_content_length", content_length)
    monkeypatch.setattr(mcp_shared, "_stdout_fd", 98765)
    monkeypatch.setattr(mcp_shared, "_write_all", lambda fd, frame: frames.append(frame))
    mcp_shared.respond("memory-recall-unicode-expansion", build_tool_response(encoded))
    assert len(frames[0]) <= MAX_RECALL_PAYLOAD_BYTES
    frame = frames[0].split(b"\r\n\r\n", 1)[-1]
    rendered = json.loads(frame)["result"]["content"][0]["text"]
    assert "\u0344" not in rendered
    result = json.loads(rendered)
    if context_cap == 200:
        assert "\u0308\u0301" in rendered
        selected = result["retrieval"][kind]
        assert selected[0]["id"] == "combining"
        assert selected[0][field]
        assert "[truncated]" in rendered
    else:
        # The reference wrapper alone exceeds this budget, so returning a
        # claimed record without its text would misrepresent the evidence.
        assert result["retrieval"][kind] == []
        assert result["retrieval"]["omitted_for_payload_budget"] == 1
    actual_chars = sum(
        len(result[f"{kind}_context"]) for kind in ("semantic", "episodic", "lessons")
    )
    assert result["total_chars"] == actual_chars <= context_cap
