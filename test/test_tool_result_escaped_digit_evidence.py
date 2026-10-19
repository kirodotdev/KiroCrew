"""A figure a serialised tool result carries as JSON escapes is still echo evidence.

A tool-result parser shows a ``Json`` item it cannot display as plain text (a
stderr-only shell result, an unrecognised envelope) as ``json.dumps`` of it, which
spells every non-ASCII character as a ``\\uXXXX`` escape. Arabic-Indic digits in
such a result therefore reach ``tool_output`` as escapes, and the banner echo
check must still read them as the number they are. Every parser that builds
tool-result evidence is covered: the dispatch terminal-frame builder, the
client's streaming update parser and the kiro-cli session-file read-back.
"""

from __future__ import annotations

import json

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers

from kiro_crew.acp import _dispatch as acp_dispatch
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.dashboard.chat_utils_turn_text import (
    _carries_count,
    classify_provider_budget_banner,
)

_EXACT = "You have 8154 weighted tokens left"
#: French for "you have 8154 tokens left", in Arabic-Indic digits.
_FRENCH = "Il vous reste \u0668\u0661\u0665\u0664 jetons"
_STDERR_ONLY = {"exit_status": "exit status: 1", "stderr": _FRENCH}


def _update(payload: dict) -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "run-script",
        "status": "completed",
        "rawOutput": {"items": [{"Json": payload}]},
    }


def _read_back(tmp_path, monkeypatch, payload: dict):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    session_dir = tmp_path / ".kiro" / "sessions" / "cli"
    session_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "kind": "ToolResults",
        "data": {
            "content": [
                {
                    "kind": "toolResult",
                    "data": {
                        "toolUseId": "tu-1",
                        "content": [{"kind": "json", "data": payload}],
                    },
                }
            ]
        },
    }
    (session_dir / "s1.jsonl").write_text(json.dumps(entry) + "\n")
    client = AcpClient(work_dir=tmp_path)
    client._session_id = "s1"
    client._jsonl_pos = 0
    [event] = client._read_new_tool_results_sync()
    return event


def _parsed_outputs(tmp_path, monkeypatch, payload: dict) -> dict[str, object]:
    built = acp_dispatch._build_tool_result_event(_update(payload))
    streamed = AcpClient(work_dir=tmp_path)._extract_tool_call_update(
        JsonRpcMessage(method="session/update", params={"update": _update(payload)})
    )
    read_back = _read_back(tmp_path, monkeypatch, payload)
    return {"dispatch": built, "streaming": streamed, "read-back": read_back}


@pytest.mark.parametrize("parser", ["dispatch", "streaming", "read-back"])
def test_every_parser_shows_escaped_digits_that_still_carry_the_count(
    tmp_path, monkeypatch, parser
) -> None:
    event = _parsed_outputs(tmp_path, monkeypatch, _STDERR_ONLY)[parser]
    assert event is not None
    # The serialised display escapes the digits and is recorded as whole.
    assert "\\u0668\\u0661\\u0665\\u0664" in event.tool_output
    assert event.tool_output_truncated is False
    assert _carries_count(event.tool_output, "8154") is True


@pytest.mark.parametrize("parser", ["dispatch", "streaming", "read-back"])
def test_every_parser_output_keeps_the_requested_translation_visible(
    tmp_path, monkeypatch, parser
) -> None:
    event = _parsed_outputs(tmp_path, monkeypatch, _STDERR_ONLY)[parser]
    assert event is not None
    assert classify_provider_budget_banner(
        _EXACT,
        "Run the script and translate its message into English",
        is_provider_recovery=False,
        is_transient_recovery=False,
        prior_visible_output=True,
        tool_results=[event.tool_output],
        echo_evidence_complete=not event.tool_output_truncated,
    ) == (_EXACT, False)


@pytest.mark.parametrize(
    ("text", "carries"),
    [
        # An escaped digit-group separator between escaped digits.
        ('"\\u0668\\u202f\\u0661\\u0665\\u0664"', True),
        # A payload serialised twice doubles the backslashes.
        ('"\\\\u0668\\\\u0661\\\\u0665\\\\u0664"', True),
        # Mathematical bold digits are outside the BMP: a surrogate pair each.
        ('"\\ud835\\udfd6\\ud835\\udfcf\\ud835\\udfd3\\ud835\\udfd2"', True),
        # An escaped no-break space before the figure is a separator, not a digit.
        ('"reste\\u00a0\\u0668\\u0661\\u0665\\u0664"', True),
        # A longer number or a decimal is still not the same whole number.
        ('"\\u0661\\u0668\\u0661\\u0665\\u0664"', False),
        ('"\\u0668\\u0661\\u0665\\u0664.\\u0662"', False),
        # A lone surrogate escape decodes to nothing and hides nothing.
        ('"\\ud835 8154"', True),
    ],
    ids=["grouped", "double-escaped", "astral", "nbsp-prefix", "longer", "decimal", "lone"],
)
def test_escaped_count_shapes(text: str, carries: bool) -> None:
    assert _carries_count(text, "8154") is carries


class TestEscapedDigitToolEvidenceEndToEnd(BannerRecoveryHelpers):
    """The runner keeps the translation a stderr-only escaped result asked for."""

    @pytest.mark.asyncio
    async def test_a_translation_of_escaped_stderr_digits_stays_visible(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        built = acp_dispatch._build_tool_result_event(_update(_STDERR_ONLY))
        assert built is not None
        result = AcpProvider._to_llm_event(built)
        prompts: list[str] = []

        async def _stream(message):
            prompts.append(message)
            if len(prompts) > 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Work finished.")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Running the script.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="run_script",
                tool_kind="execute",
                tool_call_id="run-script",
            )
            yield result
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=_EXACT)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("escaped-digit-translation")
        slot._titled = True

        await _run_chat(state, slot, "Run the script and translate its message into English")
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == ["Running the script.", _EXACT]
        assert len(prompts) == 1
        assert slot._posttoken_retry_used is False
