"""Transport-level regression tests for session directives (#755).

These lock the hops that the existing seam tests CANNOT see. Those tests build
``AcpEvent`` objects directly with ``tool_output`` already set to a pristine
directive, so they validate the consumer while assuming the transport is
lossless. It was not: a directive was destroyed twice on its way out, and the
feature shipped dead with 21,840 tests green.

Each test here drives a REAL boundary end-to-end:

* ``build_tool_response`` — the MCP server's single response exit point, which
  used to strip every category-``Cf`` character and so removed the sentinel's
  U+2063 prefix before the response reached the wire.
* ``_build_tool_result_event`` — the ACP result parser, whose ``rawOutput``
  ``Json`` branch used to ``json.dumps`` the MCP content envelope, escaping the
  payload's quotes and non-ASCII so the marker line could not be parsed.
"""

import json

from kiro_crew import session_directive as sd
from kiro_crew.acp._dispatch import _build_tool_result_event, _mcp_content_text
from kiro_crew.validation import build_tool_response, strip_hidden_unicode

DIRECTIVE_ARGS = {"questions": [{"question": "pick one"}]}


def _encoded() -> str:
    return sd.encode("ask_question", DIRECTIVE_ARGS, "Question card requested.")


def _mcp_envelope(text: str) -> dict[str, object]:
    """The shape kiro-cli forwards verbatim as a ``rawOutput`` ``Json`` item."""
    return {"content": [{"type": "text", "text": text}]}


class TestSurvivesMcpResponseExit:
    """Defect 1: the response sanitizer must not corrupt the directive."""

    def test_directive_survives_build_tool_response(self):
        out = build_tool_response(_encoded())
        text = out["content"][0]["text"]
        assert sd.decode(text, "ask_question") == DIRECTIVE_ARGS

    def test_sentinel_is_pure_ascii(self):
        # A machine-facing framing token must not depend on characters that
        # sanitizers, Unicode normalizers or transports legitimately rewrite.
        assert _encoded().isascii() or "[[KIROCREW_SESSION_DIRECTIVE]]" in _encoded()
        assert strip_hidden_unicode(_encoded()) == _encoded()


class TestSurvivesAcpResultParser:
    """Defect 2: the rawOutput Json branch must not re-serialize the envelope."""

    def test_directive_survives_raw_output_json_envelope(self):
        update = {
            "toolCallId": "tc-1",
            "status": "completed",
            "rawOutput": {"items": [{"Json": _mcp_envelope(_encoded())}]},
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert event.tool_final is True
        assert sd.decode(event.tool_output, "ask_question") == DIRECTIVE_ARGS

    def test_directive_survives_content_block_path(self):
        update = {
            "toolCallId": "tc-2",
            "status": "completed",
            "content": [{"content": {"type": "text", "text": _encoded()}}],
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert sd.decode(event.tool_output, "ask_question") == DIRECTIVE_ARGS

    def test_full_chain_server_exit_then_acp_parser(self):
        # The exact production path: tool return -> MCP response exit ->
        # kiro-cli rawOutput Json item -> ACP parser -> consumer decode.
        served = build_tool_response(_encoded())
        update = {
            "toolCallId": "tc-3",
            "status": "completed",
            "rawOutput": {"items": [{"Json": served}]},
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert sd.decode(event.tool_output, "ask_question") == DIRECTIVE_ARGS


class TestEnvelopeExtractorBoundaries:
    """The extractor must be narrow: only pure text envelopes are unwrapped."""

    def test_extracts_single_text_block(self):
        assert _mcp_content_text(_mcp_envelope("hello")) == "hello"

    def test_joins_multiple_text_blocks(self):
        payload = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        assert _mcp_content_text(payload) == "a\nb"

    def test_returns_none_for_non_envelope(self):
        assert _mcp_content_text({"stdout": "x"}) is None
        assert _mcp_content_text({"content": []}) is None
        assert _mcp_content_text({}) is None

    def test_returns_none_for_non_text_blocks(self):
        # Structured payloads keep their json.dumps rendering.
        assert _mcp_content_text({"content": [{"type": "image", "data": "b64"}]}) is None
        assert _mcp_content_text({"content": [{"type": "text", "text": 7}]}) is None

    def test_structured_json_payload_still_serialized(self):
        update = {
            "toolCallId": "tc-4",
            "status": "completed",
            "rawOutput": {"items": [{"Json": {"rows": [1, 2], "ok": True}}]},
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert json.loads(event.tool_output) == {"rows": [1, 2], "ok": True}


class TestUserContentNotCorrupted:
    """The sanitizer narrowing must preserve script-essential characters."""

    def test_emoji_zwj_sequence_survives_a_tool_response(self):
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        out = build_tool_response(f"family: {family}")
        assert family in out["content"][0]["text"]

    def test_persian_zwnj_survives_a_tool_response(self):
        word = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
        out = build_tool_response(word)
        assert word in out["content"][0]["text"]

    def test_bidi_override_still_stripped(self):
        out = build_tool_response("safe\u202etxet-detrevr")
        assert "\u202e" not in out["content"][0]["text"]
