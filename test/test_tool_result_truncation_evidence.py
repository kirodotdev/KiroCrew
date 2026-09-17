"""Every tool-result parser says when its display bound cut the text.

``AcpEvent.tool_output_truncated`` is the parser's own record that ``tool_output``
is a head cut. The chat runner's banner echo check reads a result's text for the
provider-banner phrase and treats absence as evidence; a cut result cannot supply
that evidence, so the flag is what lets the runner tell "the phrase is not there"
from "the phrase may sit past the cut". Three parsers apply a bound: the dispatch
terminal-frame builder, the client's streaming update parser, and the client's
kiro-cli session-file read-back (which also bounds each part before the join).
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.acp import _dispatch as acp_dispatch
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import EVENT_TOOL_RESULT, AcpEvent, JsonRpcMessage
from kiro_crew.session_directive import MAX_TOOL_RESULT_CHARS

_PHRASE = "You have 8154 weighted tokens left"


def _update(text: str) -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "read-long",
        "status": "completed",
        "rawOutput": {"items": [{"Text": text}]},
    }


def test_the_event_field_defaults_to_not_truncated() -> None:
    assert AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t").tool_output_truncated is False


def test_the_provider_forwards_the_flag_to_the_runner() -> None:
    """``AcpProvider._to_llm_event`` is the production seam between parser and runner.

    A field it does not copy arrives at the runner as its default, so a cut the
    parser recorded would read as whole evidence there.
    """
    from kiro_crew.providers.acp import AcpProvider

    cut = AcpEvent(
        kind=EVENT_TOOL_RESULT, tool_call_id="t", tool_output="x", tool_output_truncated=True
    )
    assert AcpProvider._to_llm_event(cut).tool_output_truncated is True
    whole = AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t", tool_output="x")
    assert AcpProvider._to_llm_event(whole).tool_output_truncated is False


@pytest.mark.parametrize(
    ("length", "truncated"),
    [
        (MAX_TOOL_RESULT_CHARS - 1, False),
        (MAX_TOOL_RESULT_CHARS, False),
        (MAX_TOOL_RESULT_CHARS + 1, True),
    ],
    ids=["under", "at", "over"],
)
def test_dispatch_builder_records_the_join_cut(length: int, truncated: bool) -> None:
    event = acp_dispatch._build_tool_result_event(_update("x" * length))
    assert event is not None
    assert len(event.tool_output) <= MAX_TOOL_RESULT_CHARS
    assert event.tool_output_truncated is truncated


def test_dispatch_builder_cut_hides_a_banner_phrase_past_the_bound() -> None:
    """The exact shape the runner must not misread: the phrase is the last line."""
    text = "x" * MAX_TOOL_RESULT_CHARS + "\n" + _PHRASE
    event = acp_dispatch._build_tool_result_event(_update(text))
    assert event is not None
    assert _PHRASE not in event.tool_output
    assert event.tool_output_truncated is True


@pytest.mark.parametrize(
    ("length", "truncated"),
    [(8000, False), (8001, True)],
    ids=["at", "over"],
)
def test_client_streaming_parser_records_the_cut(tmp_path, length: int, truncated: bool) -> None:
    msg = JsonRpcMessage(method="session/update", params={"update": _update("x" * length)})
    event = AcpClient(work_dir=tmp_path)._extract_tool_call_update(msg)
    assert event is not None
    assert len(event.tool_output) <= 8000
    assert event.tool_output_truncated is truncated


def _read_back(tmp_path, monkeypatch, parts: list[dict]) -> AcpEvent:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    session_dir = tmp_path / ".kiro" / "sessions" / "cli"
    session_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "kind": "ToolResults",
        "data": {
            "content": [
                {"kind": "toolResult", "data": {"toolUseId": "tu-1", "content": parts}},
            ]
        },
    }
    (session_dir / "s1.jsonl").write_text(json.dumps(entry) + "\n")
    client = AcpClient(work_dir=tmp_path)
    client._session_id = "s1"
    client._jsonl_pos = 0
    [event] = client._read_new_tool_results_sync()
    return event


def test_read_back_parser_records_a_per_part_cut(tmp_path, monkeypatch) -> None:
    """A part bound removes text before the join, so the joined length is not enough."""
    whole = _read_back(tmp_path, monkeypatch, [{"kind": "text", "data": "x" * 4000}])
    assert whole.tool_output_truncated is False
    cut = _read_back(tmp_path, monkeypatch, [{"kind": "text", "data": "x" * 4000 + "\n" + _PHRASE}])
    assert len(cut.tool_output) == 4000
    assert _PHRASE not in cut.tool_output
    assert cut.tool_output_truncated is True


def test_read_back_parser_records_a_cut_on_every_part_kind(tmp_path, monkeypatch) -> None:
    stdout = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": {"stdout": "y" * 4001}}])
    assert stdout.tool_output_truncated is True
    dumped = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": {"detail": "z" * 4001}}])
    assert dumped.tool_output_truncated is True


def test_read_back_parser_records_the_join_cut(tmp_path, monkeypatch) -> None:
    """Three whole parts of 3,000 join to 9,002 characters: only the join cut applies."""
    parts = [{"kind": "text", "data": "x" * 3000} for _ in range(3)]
    event = _read_back(tmp_path, monkeypatch, parts)
    assert len(event.tool_output) == 8000
    assert event.tool_output_truncated is True


# A ``Json`` result item shows stdout alone. Text the tool returned in another
# output field (stderr) is then not in ``tool_output``, and the echo check that
# reads the row text cannot see a banner phrase that sits there. The same flag
# says so; the shell envelope's ``exit_status`` string is a status, not output.
_SHELL_OK = {"exit_status": "exit status: 0", "stdout": "OK\n", "stderr": ""}
_SHELL_STDERR_BANNER = {"exit_status": "exit status: 0", "stdout": "OK\n", "stderr": _PHRASE}


def _json_update(payload: dict) -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "run-check",
        "status": "completed",
        "rawOutput": {"items": [{"Json": payload}]},
    }


def test_dispatch_builder_marks_a_stdout_display_that_leaves_out_stderr_text() -> None:
    whole = acp_dispatch._build_tool_result_event(_json_update(_SHELL_OK))
    assert whole is not None
    assert whole.tool_output == "OK\n"
    assert whole.tool_output_truncated is False
    partial = acp_dispatch._build_tool_result_event(_json_update(_SHELL_STDERR_BANNER))
    assert partial is not None
    assert partial.tool_output == "OK\n"
    assert _PHRASE not in partial.tool_output
    assert partial.tool_output_truncated is True


def test_client_streaming_parser_marks_a_stdout_display_that_leaves_out_stderr_text(
    tmp_path,
) -> None:
    client = AcpClient(work_dir=tmp_path)
    whole = client._extract_tool_call_update(
        JsonRpcMessage(method="session/update", params={"update": _json_update(_SHELL_OK)})
    )
    assert whole is not None and whole.tool_output_truncated is False
    partial = client._extract_tool_call_update(
        JsonRpcMessage(
            method="session/update", params={"update": _json_update(_SHELL_STDERR_BANNER)}
        )
    )
    assert partial is not None
    assert _PHRASE not in partial.tool_output
    assert partial.tool_output_truncated is True


def test_read_back_parser_marks_a_stdout_display_that_leaves_out_stderr_text(
    tmp_path, monkeypatch
) -> None:
    whole = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": _SHELL_OK}])
    assert whole.tool_output_truncated is False
    partial = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": _SHELL_STDERR_BANNER}])
    assert _PHRASE not in partial.tool_output
    assert partial.tool_output_truncated is True
    # An empty stdout drops the item from the display entirely; stderr text it
    # carried is still text the row never shows.
    only_stderr = _read_back(
        tmp_path,
        monkeypatch,
        [
            {"kind": "text", "data": "ran"},
            {
                "kind": "json",
                "data": {"exit_status": "exit status: 1", "stdout": "", "stderr": _PHRASE},
            },
        ],
    )
    assert _PHRASE not in only_stderr.tool_output
    assert only_stderr.tool_output_truncated is True


def test_a_status_only_envelope_is_whole() -> None:
    """``exit_status`` is spelled as a string by kiro-cli and is not output text."""
    assert acp_dispatch._json_result_omits_text(_SHELL_OK) is False
    assert (
        acp_dispatch._json_result_omits_text({"exit_status": "exit status: 1", "stdout": "x"})
        is False
    )
    assert acp_dispatch._json_result_omits_text({"stdout": "x", "stderr": "   "}) is False
    assert acp_dispatch._json_result_omits_text({"stdout": "x", "stderr": "warning"}) is True
