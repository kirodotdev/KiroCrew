"""A tool result whose payload is only in ``structuredContent`` gets it as a text block.

The engines Crew runs read only ``content`` for the model, so a server that answers
``content: []`` beside a ``structuredContent`` object (Airtable's hosted server, once
the client sends ``MCP-Protocol-Version``) reached the agent as a call with no output.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import backend as backend_facade
from kiro_crew.mcp_gateway import backend_routing
from kiro_crew.mcp_structured_text import (
    add_text_to_line,
    line_may_need_text,
    with_structured_text,
)

STRUCTURED = {"bases": [{"id": "app1", "name": "Ü"}]}
TEXT = json.dumps(STRUCTURED, ensure_ascii=False, separators=(",", ":"))


def _line(result: dict, **extra) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 3, "result": result, **extra}).encode() + b"\n"


class TestWithStructuredText:
    @pytest.mark.parametrize(
        "result",
        [
            {"content": [], "structuredContent": STRUCTURED},
            {"structuredContent": STRUCTURED},
        ],
        ids=["empty-content", "no-content"],
    )
    def test_the_object_is_added_as_json_text_and_kept(self, result):
        out = with_structured_text(result)
        assert out is not None
        assert out["content"] == [{"type": "text", "text": TEXT}]
        assert out["structuredContent"] == STRUCTURED, "kept for the outputSchema check"

    def test_a_non_text_block_is_kept_ahead_of_the_added_text(self):
        image = {"type": "image", "data": "AA==", "mimeType": "image/png"}
        out = with_structured_text({"content": [image], "structuredContent": STRUCTURED})
        assert out is not None and out["content"] == [image, {"type": "text", "text": TEXT}]

    @pytest.mark.parametrize(
        "result",
        [
            {"content": [{"type": "text", "text": "copy"}], "structuredContent": STRUCTURED},
            {"content": [{"type": "text", "text": "pong"}]},
            {"content": [], "structuredContent": ["not", "an", "object"]},
            {"content": [], "structuredContent": None},
            {"content": "not a list", "structuredContent": STRUCTURED},
            "not an object",
            None,
        ],
        ids=["has-text", "no-structured", "list", "null", "bad-content", "str", "none"],
    )
    def test_nothing_is_added(self, result):
        assert with_structured_text(result) is None


class TestLine:
    def test_a_response_line_gets_the_text(self):
        out = json.loads(add_text_to_line(_line({"content": [], "structuredContent": STRUCTURED})))
        assert out["id"] == 3 and out["result"]["content"] == [{"type": "text", "text": TEXT}]

    @pytest.mark.parametrize(
        "line",
        [
            _line({"content": [{"type": "text", "text": "x"}], "structuredContent": STRUCTURED}),
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "x", "result": {"structuredContent": {}}}
            ).encode(),
            b'{"structuredContent": not json',
        ],
        ids=["has-text", "a-request", "unparseable"],
    )
    def test_any_other_line_is_returned_as_is(self, line):
        assert add_text_to_line(line) is line

    def test_the_probe_passes_only_a_line_naming_the_key(self):
        assert line_may_need_text(_line({"content": [], "structuredContent": STRUCTURED}))
        assert not line_may_need_text(_line({"content": [{"type": "text", "text": "pong"}]}))


def _pump(line: bytes) -> list[bytes]:
    routed: list[bytes] = []

    async def _route(routed_line: bytes) -> None:
        routed.append(routed_line)

    backend = SimpleNamespace(pool_key=SimpleNamespace(server_name="airtable"))
    backend._route_backend_line = _route
    asyncio.run(asyncio.wait_for(backend_routing._handle_stdout_line(backend, line), 30))
    return routed


class TestGatewayPump:
    def test_a_structured_only_result_is_routed_with_its_text(self):
        (routed,) = _pump(_line({"content": [], "structuredContent": STRUCTURED}))
        result = json.loads(routed)["result"]
        assert result["content"] == [{"type": "text", "text": TEXT}]
        assert result["structuredContent"] == STRUCTURED

    def test_an_image_omission_notice_does_not_stand_in_for_the_payload(self):
        # The image budget swaps an undecodable image for a text notice; the
        # structured payload must still arrive as its own text block.
        image = {"type": "image", "data": "bm90IGFuIGltYWdl", "mimeType": "image/png"}
        (routed,) = _pump(_line({"content": [image], "structuredContent": STRUCTURED}))
        texts = [b["text"] for b in json.loads(routed)["result"]["content"] if b["type"] == "text"]
        assert TEXT in texts
        assert len(texts) == 2, "the omission notice is there too"

    def test_the_added_text_is_spilled_like_any_other(self, monkeypatch, tmp_path):
        # The size cap still holds: the text block is added BEFORE the spill step.
        monkeypatch.setattr(backend_facade, "RESPONSE_SPILL_THRESHOLD_BYTES", 1024)
        monkeypatch.setattr("kiro_crew.mcp_gateway.spill.config_dir", lambda: tmp_path)
        big = {"rows": ["r" * 40_000]}
        (routed,) = _pump(_line({"content": [], "structuredContent": big}))
        (block,) = json.loads(routed)["result"]["content"]
        assert "response truncated" in block["text"]
        assert len(block["text"].encode()) < 20_000
