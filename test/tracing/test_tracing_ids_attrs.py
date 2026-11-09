"""The deterministic id formula, the attribute allowlist and the JSONL span sink."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kiro_crew.tracing import correlation, ids

_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "tracing" / "id_conformance.json"


def _vectors():
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return [(doc["namespace"], v) for v in doc["vectors"]]


@pytest.mark.parametrize(("ns", "vector"), _vectors())
def test_every_conformance_vector_reproduces(ns, vector):
    fn = getattr(ids, vector["fn"])
    parts = vector["parts"]
    got = fn(ns, parts[0]) if vector["fn"] == "turn_trace_id" else fn(ns, vector["kind"], *parts)
    assert got == vector["expected"]


def test_the_formula_is_the_one_the_spec_spells_out():
    # Spelled out by hand, so a change to ids.py cannot silently move both sides.
    ns = "00112233445566778899aabbccddeeff"
    material = f"kirocrew.v1|{ns}|tool|sess-a|tooluse_1".encode("utf-8")
    assert ids.span_id(ns, "tool", "sess-a", "tooluse_1") == (
        hashlib.sha256(material).hexdigest()[:16]
    )
    trace_material = f"kirocrew.v1|{ns}|trace.turn|x".encode("utf-8")
    assert ids.trace_id(ns, "turn", "x") == hashlib.sha256(trace_material).hexdigest()[:32]


def test_a_pipe_inside_a_part_cannot_collide_with_a_boundary():
    ns = "0" * 31 + "1"
    assert ids.span_id(ns, "tool", "a|b", "c") != ids.span_id(ns, "tool", "a", "b|c")
    assert ids.esc("100%|") == "100%25%7C"


def test_the_same_tool_call_id_in_two_sessions_gets_two_ids():
    ns = "0" * 31 + "1"
    assert ids.span_id(ns, "tool", "s1", "t1") != ids.span_id(ns, "tool", "s2", "t1")


def test_another_namespace_gives_other_ids():
    assert ids.span_id("a" * 32, "turn", "t", 1) != ids.span_id("b" * 32, "turn", "t", 1)


def test_a_turn_id_is_its_own_trace_id_and_anything_else_is_hashed():
    ns = "c" * 32
    turn = "0123456789abcdef0123456789abcdef"
    assert ids.turn_trace_id(ns, turn) == turn
    hashed = ids.turn_trace_id(ns, "turn-1")
    assert len(hashed) == 32 and hashed == ids.trace_id(ns, "turn", "turn-1")
    assert ids.turn_trace_id(ns, "0" * 32) != "0" * 32


def test_the_namespace_is_minted_once_and_then_read(tmp_path):
    first = ids.load_namespace(tmp_path / "traces")
    assert len(first) == 32 and int(first, 16) >= 0
    assert ids.load_namespace(tmp_path / "traces") == first


def test_an_existing_namespace_wins_and_no_staging_file_is_left(tmp_path, monkeypatch):
    # A racer that published first: this process's link fails and it reads the winner.
    winner = "ab" * 16
    real_link = ids.os.link

    def link_after_racer(src, dst):
        Path(dst).write_text(winner + "\n", encoding="utf-8")
        return real_link(src, dst)

    monkeypatch.setattr(ids.os, "link", link_after_racer)
    assert ids.load_namespace(tmp_path) == winner
    assert [p.name for p in tmp_path.iterdir()] == [ids.NAMESPACE_FILE]


def test_a_malformed_namespace_file_is_refused(tmp_path):
    (tmp_path / ids.NAMESPACE_FILE).write_text("not hex", encoding="utf-8")
    with pytest.raises(ValueError):
        ids.load_namespace(tmp_path)


def test_the_attribute_allowlist_is_pinned():
    # Adding a key is a reviewed change: it puts a new field on every exported span.
    assert sorted(correlation.ALLOWED_KEYS) == sorted(
        [
            "error.type",
            "gen_ai.agent.name",
            "gen_ai.operation.name",
            "gen_ai.request.model",
            "gen_ai.response.model",
            "gen_ai.tool.name",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "kirocrew.acp_session_id",
            "kirocrew.attempt",
            "kirocrew.chat.aggregate",
            "kirocrew.crew.dispatcher",
            "kirocrew.crew.root",
            "kirocrew.crew.root_kind",
            "kirocrew.local.session_key",
            "kirocrew.parent_session_key_hash",
            "kirocrew.run.continuation",
            "kirocrew.run.kind",
            "kirocrew.run.status",
            "kirocrew.session_key_hash",
            "kirocrew.stop_reason",
            "kirocrew.subagent_id",
            "kirocrew.tool.outcome",
            "kirocrew.tool_call_id",
            "kirocrew.tool_kind",
            "kirocrew.turn_id",
            "kirocrew.usage.complete",
            "kirocrew.usage.cost_usd",
            "kirocrew.usage.cost_usd_total",
            "kirocrew.usage.credits",
            "kirocrew.usage.credits_total",
            "kirocrew.usage.input_tokens_total",
            "kirocrew.usage.model_calls",
            "kirocrew.usage.output_tokens_total",
            "kirocrew.usage.turns_total",
            "kirocrew.work.conductor_hash",
            "kirocrew.work.item_id",
            "kirocrew.work.round",
            "langfuse.observation.cost_details",
            "langfuse.observation.type",
            "langfuse.observation.usage_details",
            "session.id",
        ]
    )


def test_clean_drops_unlisted_keys_empty_values_and_malformed_ids():
    cleaned = correlation.clean(
        {
            "gen_ai.prompt": "hello",
            "kirocrew.tool_call_id": "tooluse_Xy7Kq2LmN9pR4sT1vW8zB3",
            "kirocrew.turn_id": "has a space",
            "gen_ai.request.model": "",
            "kirocrew.stop_reason": None,
            "kirocrew.attempt": 2,
        }
    )
    # A backend's random id survives: the content scrubber would blank it.
    assert cleaned == {
        "kirocrew.tool_call_id": "tooluse_Xy7Kq2LmN9pR4sT1vW8zB3",
        "kirocrew.attempt": 2,
    }


def test_a_credential_in_a_free_text_value_is_scrubbed():
    # Split so the literal is not itself a credential-shaped line in the repo.
    cleaned = correlation.clean({"gen_ai.agent.name": "AKIA" + "ABCDEFGHIJKLMNOP"})
    assert "AKIA" not in json.dumps(cleaned)


def test_session_keys_are_hashed_with_the_raw_key_kept_local():
    attrs = correlation.session_attrs(b"k" * 32, "dashboard_chat-1", "dashboard_chat-1")
    assert attrs["kirocrew.local.session_key"] == "dashboard_chat-1"
    assert attrs["session.id"] == attrs["kirocrew.session_key_hash"]
    assert "dashboard_chat-1" not in (attrs["session.id"], attrs["kirocrew.session_key_hash"])
    assert len(attrs["session.id"]) == correlation.HASH_HEX_LEN
    # Same key, same hash: stable across restarts of one install.
    assert correlation.session_attrs(b"k" * 32, "dashboard_chat-1", "")["session.id"] == (
        attrs["session.id"]
    )
    assert correlation.session_attrs(None, "dashboard_chat-1", "") == {
        "kirocrew.local.session_key": "dashboard_chat-1"
    }


def test_the_jsonl_sink_writes_one_line_per_span_and_shuts_down(tmp_path):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from kiro_crew.tracing.exporter import JsonlSpanExporter

    exporter = JsonlSpanExporter(tmp_path, retention_days=1, max_total_mb=1)
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tp.get_tracer("t")
    for name in ("a", "b"):
        tracer.start_span(name, attributes={"kirocrew.local.session_key": "s"}).end()
    tp.shutdown()
    shards = list(tmp_path.glob("spans-*.jsonl"))
    assert len(shards) == 1
    lines = shards[0].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["name"] for line in lines] == ["a", "b"]
    # The local sink keeps the raw form; nothing here sends it anywhere.
    assert json.loads(lines[0])["attributes"]["kirocrew.local.session_key"] == "s"
    assert exporter.force_flush() is True


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0123456789abcdef" * 2, True),
        ("0123456789ABCDEF" * 2, False),
        ("a" * 31, False),
        ("a" * 33, False),
        ("g" * 32, False),
        ("", False),
        ("a" * 31 + "A", False),  # one uppercase digit
        ("a" * 31 + "é", False),  # non-ASCII
        ("a" * 31 + "\n", False),  # trailing newline, still 32 chars
        ("a" * 32 + "\n", False),
        ("a" * 31 + "٣", False),  # a non-ASCII digit str.isdigit() accepts
    ],
)
def test_is_hex32_accepts_exactly_32_lowercase_hex(value, ok):
    assert ids.is_hex32(value) is ok


def test_turn_trace_id_and_load_namespace_use_the_strict_hex_check(tmp_path):
    ns = "c" * 32
    assert ids.turn_trace_id(ns, "a" * 32) == "a" * 32
    # Not 32 lowercase hex: hashed into a trace id, never passed through.
    for value in ("a" * 31 + "\n", "a" * 31 + "é", "a" * 33):
        assert ids.turn_trace_id(ns, value) == ids.trace_id(ns, "turn", value)
    (tmp_path / ids.NAMESPACE_FILE).write_text("a" * 31 + "g\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ids.load_namespace(tmp_path)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("tooluse_A1:b@c/d+e=f-g.h", True),
        ("x" * 256, True),
        ("x" * 257, False),
        ("", False),
        ("has space", False),
        ("tab\t", False),
        ("ünicode", False),
        ("tooluse_1\n", False),  # trailing newline
        ("x" * 255 + "\n", False),
        ("tool٣", False),  # a non-ASCII digit str.isalnum() accepts
    ],
)
def test_an_id_is_bounded_to_its_alphabet_and_length(value, ok):
    assert correlation._is_id(value) is ok


def test_clean_drops_an_id_that_fails_the_check():
    attrs = {"kirocrew.tool_call_id": "t1\n", "kirocrew.turn_id": "x" * 257}
    assert correlation.clean(attrs) == {}
    assert correlation.clean({"kirocrew.tool_call_id": "t1"}) == {"kirocrew.tool_call_id": "t1"}
