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


def _read_back_list(tmp_path, monkeypatch, parts: list[dict]) -> list[AcpEvent]:
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
    return client._read_new_tool_results_sync()


def _read_back(tmp_path, monkeypatch, parts: list[dict]) -> AcpEvent:
    [event] = _read_back_list(tmp_path, monkeypatch, parts)
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


# The other output-bearing fields of a ``Json`` item are not only strings: a
# nested object or list, and a number, are output the stdout display leaves out
# too. A boolean, a null and an empty container hold nothing a reader could echo.
_SHELL_REPORT_BANNER = {"stdout": "OK", "report": {"sentence": _PHRASE}}


def test_a_container_or_number_beside_stdout_is_omitted_output() -> None:
    assert acp_dispatch._json_result_omits_text(_SHELL_REPORT_BANNER) is True
    assert acp_dispatch._json_result_omits_text({"stdout": "OK", "count": 8154}) is True
    assert acp_dispatch._json_result_omits_text({"stdout": "OK", "lines": ["x"]}) is True
    assert (
        acp_dispatch._json_result_omits_text(
            {"stdout": "OK", "ok": True, "note": None, "items": [], "extra": {}}
        )
        is False
    )
    assert acp_dispatch._json_result_omits_text({"stdout": "OK", "exit_code": 0}) is False


def test_every_parser_marks_a_stdout_display_that_leaves_out_a_container(
    tmp_path, monkeypatch
) -> None:
    built = acp_dispatch._build_tool_result_event(_json_update(_SHELL_REPORT_BANNER))
    assert built is not None and built.tool_output == "OK"
    assert built.tool_output_truncated is True
    streamed = AcpClient(work_dir=tmp_path)._extract_tool_call_update(
        JsonRpcMessage(
            method="session/update", params={"update": _json_update(_SHELL_REPORT_BANNER)}
        )
    )
    assert streamed is not None and _PHRASE not in streamed.tool_output
    assert streamed.tool_output_truncated is True
    read_back = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": _SHELL_REPORT_BANNER}])
    assert _PHRASE not in read_back.tool_output
    assert read_back.tool_output_truncated is True


# kiro-cli forwards an MCP ``tools/call`` result verbatim as a ``Json`` item, and
# a ``rawOutput`` may be such an envelope itself or carry one under ``result``. An
# image block in it renders as encoded bytes at best, never as the figure it
# depicts, however small the image is, so the result is not whole.
_MCP_IMAGE = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
_MCP_IMAGE_ENVELOPE = {"content": [_MCP_IMAGE]}
_MCP_TEXT_ENVELOPE = {"content": [{"type": "text", "text": "done"}]}


def test_every_parser_marks_a_json_wrapped_mcp_image(tmp_path, monkeypatch) -> None:
    assert acp_dispatch._json_result_holds_image(_MCP_IMAGE_ENVELOPE) is True
    assert acp_dispatch._json_result_holds_image(_MCP_TEXT_ENVELOPE) is False
    assert acp_dispatch._json_result_holds_image({"content": "not a list"}) is False
    built = acp_dispatch._build_tool_result_event(_json_update(_MCP_IMAGE_ENVELOPE))
    assert built is not None and built.tool_output_truncated is True
    whole = acp_dispatch._build_tool_result_event(_json_update(_MCP_TEXT_ENVELOPE))
    assert whole is not None and whole.tool_output == "done"
    assert whole.tool_output_truncated is False
    streamed = AcpClient(work_dir=tmp_path)._extract_tool_call_update(
        JsonRpcMessage(
            method="session/update", params={"update": _json_update(_MCP_IMAGE_ENVELOPE)}
        )
    )
    assert streamed is not None and streamed.tool_output_truncated is True
    read_back = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": _MCP_IMAGE_ENVELOPE}])
    assert read_back.tool_output_truncated is True


@pytest.mark.parametrize(
    "raw_output",
    [_MCP_IMAGE_ENVELOPE, {"result": _MCP_IMAGE_ENVELOPE}],
    ids=["bare-envelope", "result-wrapped"],
)
def test_a_raw_output_mcp_image_is_marked_by_both_streaming_parsers(tmp_path, raw_output) -> None:
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "screenshot",
        "status": "completed",
        "rawOutput": raw_output,
    }
    built = acp_dispatch._build_tool_result_event(update)
    assert built is not None and built.tool_output_truncated is True
    streamed = AcpClient(work_dir=tmp_path)._extract_tool_call_update(
        JsonRpcMessage(method="session/update", params={"update": update})
    )
    assert streamed is not None and streamed.tool_output_truncated is True


def test_a_status_only_envelope_is_whole() -> None:
    """``exit_status`` is spelled as a string by kiro-cli and is not output text."""
    assert acp_dispatch._json_result_omits_text(_SHELL_OK) is False
    assert (
        acp_dispatch._json_result_omits_text({"exit_status": "exit status: 1", "stdout": "x"})
        is False
    )
    assert acp_dispatch._json_result_omits_text({"stdout": "x", "stderr": "   "}) is False
    assert acp_dispatch._json_result_omits_text({"stdout": "x", "stderr": "warning"}) is True


# A status field is set aside only while it reads as a run status. A free-text
# ``status`` beside ``stdout`` is output the display leaves out, in every parser
# that shows the item as its stdout.
@pytest.mark.parametrize(
    ("status_field", "omits"),
    [
        ({"exit_status": "exit status: 0"}, False),
        ({"exit_code": 0}, False),
        ({"returncode": -15}, False),
        ({"exit_code": "127"}, False),
        ({"status": "success"}, False),
        ({"status": "Completed"}, False),
        ({"status": None}, False),
        ({"status": "Il vous reste 8154 jetons pondérés"}, True),
        ({"status": _PHRASE}, True),
        ({"exit_code": 8154}, True),
        ({"status": 8154}, True),
        ({"status": {"line": _PHRASE}}, True),
    ],
    ids=[
        "shell-exit-status",
        "int-exit-code",
        "negative-returncode",
        "string-exit-code",
        "status-word",
        "status-word-capitalized",
        "null-status",
        "free-text-status",
        "banner-status",
        "out-of-range-exit-code",
        "number-status",
        "object-status",
    ],
)
def test_a_status_field_is_set_aside_only_while_it_reads_as_a_run_status(
    status_field: dict, omits: bool
) -> None:
    assert acp_dispatch._json_result_omits_text({"stdout": "OK", **status_field}) is omits


def test_every_parser_marks_a_free_text_status_beside_stdout(tmp_path, monkeypatch) -> None:
    payload = {"stdout": "OK", "status": _PHRASE}
    built = acp_dispatch._build_tool_result_event(_json_update(payload))
    assert built is not None and built.tool_output == "OK"
    assert built.tool_output_truncated is True
    streamed = AcpClient(work_dir=tmp_path)._extract_tool_call_update(
        JsonRpcMessage(method="session/update", params={"update": _json_update(payload)})
    )
    assert streamed is not None and _PHRASE not in streamed.tool_output
    assert streamed.tool_output_truncated is True
    read_back = _read_back(tmp_path, monkeypatch, [{"kind": "json", "data": payload}])
    assert _PHRASE not in read_back.tool_output
    assert read_back.tool_output_truncated is True


# The dispatch builder shows a pure-text MCP envelope as its text blocks alone
# (the client parsers dump the whole item instead), so an output-bearing field
# beside ``content`` is output the row never shows: ``structuredContent`` above
# all. ``isError`` is a boolean, which holds nothing a reader could echo.
_MCP_TEXT_WITH_STRUCTURED = {
    "content": [{"type": "text", "text": "report ready"}],
    "structuredContent": {"line": "Il vous reste 8154 jetons pondérés"},
}


def test_the_dispatch_builder_marks_an_mcp_field_its_text_display_drops() -> None:
    assert acp_dispatch._mcp_result_omits_output(_MCP_TEXT_WITH_STRUCTURED) is True
    assert acp_dispatch._mcp_result_omits_output({**_MCP_TEXT_ENVELOPE, "isError": False}) is False
    built = acp_dispatch._build_tool_result_event(_json_update(_MCP_TEXT_WITH_STRUCTURED))
    assert built is not None and built.tool_output == "report ready"
    assert built.tool_output_truncated is True
    whole = acp_dispatch._build_tool_result_event(
        _json_update({**_MCP_TEXT_ENVELOPE, "isError": False})
    )
    assert whole is not None and whole.tool_output == "done"
    assert whole.tool_output_truncated is False


@pytest.mark.parametrize(
    ("raw_output", "omitted"),
    [
        ({"result": _MCP_TEXT_WITH_STRUCTURED, "error": None}, True),
        (
            {"result": _MCP_TEXT_ENVELOPE, "error": None, "note": "Il vous reste 8154 jetons"},
            True,
        ),
        ({"result": _MCP_TEXT_ENVELOPE, "error": None}, False),
    ],
    ids=["result-field", "wrapper-field", "text-only"],
)
def test_a_result_wrapped_mcp_text_display_marks_what_it_drops(
    raw_output: dict, omitted: bool
) -> None:
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "codex-mcp",
        "status": "completed",
        "rawOutput": raw_output,
    }
    built = acp_dispatch._build_tool_result_event(update)
    assert built is not None
    assert "8154" not in built.tool_output
    assert built.tool_output_truncated is omitted


# An image content block renders no text, so a tool result that carries one is
# not whole: its pixels (and any figure they depict) are absent from
# ``tool_output``. The same flag says so, and an image-only result -- which has
# no text part to shorten -- is marked incomplete on that signal alone, so the
# echo check never reads "the phrase is not there" from a result it could not see.
_IMG_WRAPPED = {
    "type": "content",
    "content": {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"},
}
_IMG_BARE = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
_TEXT_BLOCK = {"type": "content", "content": {"type": "text", "text": "see screenshot"}}


def _content_update(blocks: list[dict]) -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "screenshot",
        "status": "completed",
        "content": blocks,
    }


def test_tool_call_content_is_image_reads_the_wrapped_and_bare_shapes() -> None:
    assert acp_dispatch.tool_call_content_is_image(_IMG_WRAPPED) is True
    assert acp_dispatch.tool_call_content_is_image(_IMG_BARE) is True
    assert acp_dispatch.tool_call_content_is_image(_TEXT_BLOCK) is False
    assert acp_dispatch.tool_call_content_is_image({"type": "text", "text": "x"}) is False
    assert acp_dispatch.tool_call_content_is_image("not a dict") is False


def test_dispatch_builder_marks_a_dropped_image_part() -> None:
    event = acp_dispatch._build_tool_result_event(_content_update([_TEXT_BLOCK, _IMG_WRAPPED]))
    assert event is not None
    assert event.tool_output == "see screenshot"
    assert event.tool_output_truncated is True


def test_dispatch_builder_marks_an_image_only_result() -> None:
    event = acp_dispatch._build_tool_result_event(_content_update([_IMG_BARE]))
    assert event is not None
    assert event.tool_output_truncated is True


def test_client_streaming_parser_marks_a_dropped_image_part(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path)
    both = client._extract_tool_call_update(
        JsonRpcMessage(
            method="session/update",
            params={"update": _content_update([_TEXT_BLOCK, _IMG_WRAPPED])},
        )
    )
    assert both is not None and both.tool_output_truncated is True
    image_only = client._extract_tool_call_update(
        JsonRpcMessage(method="session/update", params={"update": _content_update([_IMG_BARE])})
    )
    assert image_only is not None and image_only.tool_output_truncated is True


def test_read_back_parser_marks_a_dropped_image_part(tmp_path, monkeypatch) -> None:
    both = _read_back(
        tmp_path,
        monkeypatch,
        [{"kind": "text", "data": "see shot"}, {"kind": "image", "data": "iVBORw0KGgo="}],
    )
    assert both.tool_output == "see shot"
    assert both.tool_output_truncated is True


def test_read_back_parser_marks_an_image_only_result(tmp_path, monkeypatch) -> None:
    events = _read_back_list(tmp_path, monkeypatch, [{"kind": "image", "data": "iVBORw0KGgo="}])
    assert len(events) == 1
    assert events[0].tool_output == ""
    assert events[0].tool_output_truncated is True
