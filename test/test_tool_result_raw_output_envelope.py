"""``rawOutput`` shapes that are not kiro-cli's ``items[]`` envelope.

Every payload below was captured from a live ACP session, not invented: the
``items[]`` shapes from ``kiro-cli acp`` (2.21.0) and the flat shapes from the
same binary's KAS relay (``--agent-engine v3``, KAS 0.54.8) driven by a
hand-rolled ACP client. Frame capture for issue #7799.

The distinction these pin: ``rawOutput`` is unstructured passthrough, so an
object Crew does not recognise is NOT evidence the tool produced no output.
``_build_tool_result_event`` returning ``None`` drops the whole
``EVENT_TOOL_RESULT``, which is also what writes ``meta["output"]`` and
``meta["done"]`` for the pill.
"""

from typing import Any

from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.types import EVENT_TOOL_RESULT


def _results(update: dict[str, Any]) -> list:
    return [e for e in parse_session_update(update) if e.kind == EVENT_TOOL_RESULT]


# ── Captured frames ──

# KAS, `fetch_cloud_config` — the first tool call of every KAS session. Its
# completion carries NO `content` array; `rawOutput` is the only carrier.
KAS_RAW_OUTPUT_ONLY: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "8b7e92c3-6d1f-4e95-a733-bcd249e18892",
    "status": "completed",
    "rawOutput": {"kind": "notEnabled", "retracted": False},
}

# KAS, `run_command` — completion carries BOTH a content block and a flat
# `rawOutput`. Content is the richer carrier and must keep winning.
KAS_CONTENT_AND_RAW_OUTPUT: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "run_command_toolu_bdrk_01FbUJo9D9aA5ovSndm3WnL4",
    "status": "completed",
    "title": "Run Command",
    "rawInput": {"command": "echo KASPROBE123", "run_in_background": False},
    "rawOutput": {
        "output": "KASPROBE123\n",
        "exitCode": 0,
        "message": "Output:\nKASPROBE123\n\n\nExit Code: 0",
    },
    "content": [
        {
            "type": "content",
            "content": {
                "type": "text",
                "text": '{"output":"KASPROBE123\\n","exitCode":0}',
            },
        }
    ],
    "_meta": {"kiro": {"toolOrigin": "default"}},
}

# kiro-cli, `shell` — the envelope the parser was written against.
CLI_ITEMS_ENVELOPE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_bdrk_0111f2nmDSPeavP9RXgFMvqT",
    "status": "completed",
    "title": "Running: echo KASPROBE123",
    "kind": "execute",
    "rawOutput": {
        "items": [
            {"Json": {"exit_status": "exit status: 0", "stdout": "KASPROBE123\n", "stderr": ""}}
        ]
    },
}


class TestUnrecognisedRawOutputIsNotAbsentOutput:
    def test_kas_raw_output_only_completion_still_emits_a_result(self) -> None:
        """The reported symptom's real half: no event at all is emitted today."""
        events = _results(KAS_RAW_OUTPUT_ONLY)
        assert len(events) == 1
        assert events[0].tool_call_id == "8b7e92c3-6d1f-4e95-a733-bcd249e18892"
        assert events[0].tool_final is True

    def test_kas_raw_output_only_payload_reaches_the_output_field(self) -> None:
        out = _results(KAS_RAW_OUTPUT_ONLY)[0].tool_output
        assert "notEnabled" in out
        assert "retracted" in out

    def test_content_block_still_wins_over_raw_output(self) -> None:
        """Path 1 precedence: the dumped envelope must not be appended too."""
        out = _results(KAS_CONTENT_AND_RAW_OUTPUT)[0].tool_output
        assert out == '{"output":"KASPROBE123\\n","exitCode":0}'
        assert "exitCode" in out
        assert "run_in_background" not in out


class TestKiroCliEnvelopeSpaceUnchanged:
    """The fallback is gated on the ABSENCE of ``items``, so no kiro-cli
    envelope — including one that legitimately yields nothing — changes."""

    def test_items_envelope_output_unchanged(self) -> None:
        out = _results(CLI_ITEMS_ENVELOPE)[0].tool_output
        assert out == "KASPROBE123\n"

    def test_empty_items_envelope_still_emits_no_result(self) -> None:
        upd = dict(CLI_ITEMS_ENVELOPE)
        upd["rawOutput"] = {"items": []}
        assert _results(upd) == []

    def test_items_envelope_with_empty_text_still_emits_no_result(self) -> None:
        upd = dict(CLI_ITEMS_ENVELOPE)
        upd["rawOutput"] = {"items": [{"Text": ""}]}
        assert _results(upd) == []

    def test_empty_raw_output_dict_emits_no_result(self) -> None:
        upd = dict(CLI_ITEMS_ENVELOPE)
        upd["rawOutput"] = {}
        assert _results(upd) == []

    def test_missing_raw_output_emits_no_result(self) -> None:
        upd = {k: v for k, v in CLI_ITEMS_ENVELOPE.items() if k != "rawOutput"}
        assert _results(upd) == []
