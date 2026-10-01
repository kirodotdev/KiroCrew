"""``parse_json_object_line``: one child-stdout line in, an object or "skip" out.

The readers built on it differ in what a skipped line costs them, so this pins
only the helper's own contract: every unusable line is ``None``, nothing it is
given raises, and ``None`` is never how end of stream is spelled.
"""

from __future__ import annotations

import pytest
from stray_line_helpers import STRAY_LINES, too_deep_to_decode

from kiro_crew import json_line
from kiro_crew.json_line import (
    parse_json_object_line,
    recover_line_id,
    recover_top_level_id,
)

_UNUSABLE = {"empty": lambda: b"", "blank": lambda: b"   \n", **STRAY_LINES}


@pytest.mark.parametrize("name", sorted(_UNUSABLE))
def test_an_unusable_line_is_none(name):
    line = _UNUSABLE[name]()
    assert parse_json_object_line(line) is None
    assert parse_json_object_line(line.decode("utf-8", "replace")) is None


def test_a_line_nested_past_the_decoder_is_none_not_a_recursion_error():
    """``RecursionError`` is a ``RuntimeError``: a ``ValueError`` arm alone misses it."""
    deep = too_deep_to_decode()
    assert parse_json_object_line(deep) is None
    assert parse_json_object_line(deep.encode("ascii")) is None


@pytest.mark.parametrize("framing", [" ", "\t", "\r\n", "\x1e", "\x0c", "\u2028", "\xa0"])
def test_an_object_inside_whitespace_framing_is_parsed(framing):
    """``str.strip`` whitespace, the json-seq record separator included."""
    assert parse_json_object_line(f'{framing}{{"id": 1}}{framing}') == {"id": 1}


def test_bytes_and_str_parse_alike():
    assert parse_json_object_line(b'{"a": "\xc3\xa9"}\n') == {"a": "é"}
    assert parse_json_object_line('{"a": "é"}\n') == {"a": "é"}


def test_errors_replace_parses_what_decodes():
    assert parse_json_object_line(b'{"a": "\xff"}', errors="replace") == {"a": "�"}
    assert parse_json_object_line(b'{"a": "\xff"}') is None


def test_a_newline_terminated_line_is_parsed_without_a_copy(monkeypatch):
    """Readers hand over lines of up to tens of MiB: stripping before the parse
    would copy every one of them, a second full-size buffer on the shared pump."""
    seen: list[str] = []
    real_loads = json_line.json.loads

    def _spy(text, *args, **kwargs):
        seen.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(json_line.json, "loads", _spy)
    line = '{"id": 1}\n'
    assert parse_json_object_line(line) == {"id": 1}
    assert len(seen) == 1 and seen[0] is line


# --- the top-level id of a message that cannot be parsed whole ---------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"jsonrpc":"2.0","id":"gw-1","result":{}}', "gw-1"),
        ('{"id": 7, "result": {}}', 7),
        # The MCP TypeScript SDK writes the id last.
        ('{"result":{"x":1},"jsonrpc":"2.0","id":"gw-2"}', "gw-2"),
        # An id inside params/result is someone else's.
        ('{"method":"log","params":{"data":{"id":"gw-3"}}}', None),
        ('{"result":{"id":"rec-1"},"jsonrpc":"2.0","id":"gw-4"}', "gw-4"),
        ('{"result":[{"id":"rec-1"}],"id":"gw-5"}', "gw-5"),
        # A string that looks like structure is still a string.
        ('{"result":"}, \\"id\\": \\"x\\"","id":"gw-6"}', "gw-6"),
        ('{"key":"id","id":null}', None),
        ('{"id":{"n":1}}', None),
        ("[1]", None),
        ("not json", None),
        # A head cut off mid-string: nothing past the cut is structure.
        ('{"result":"abc', None),
        ('  {"id":"gw-7"', "gw-7"),
    ],
)
def test_the_top_level_id_is_found_and_a_nested_one_never(text, expected):
    assert json_line._scan_top_level(text)[0] == expected


def test_the_id_scan_reaches_past_the_decoder_ceiling():
    deep = too_deep_to_decode()
    line = (
        '{"result":{"meta":{"id":"rec-1"},"x":' + deep + '},"jsonrpc":"2.0","id":"gw-8"}'
    ).encode()
    assert parse_json_object_line(line) is None
    assert recover_top_level_id(line) == "gw-8"


def test_recovery_uses_the_tail_when_the_head_lacks_the_id():
    head = b'{"result":{"content":"xxxx'
    assert recover_top_level_id(head) is None
    assert recover_top_level_id(head, b'xxxx"},"jsonrpc":"2.0","id":"gw-9"}\n') == "gw-9"
    # A nested object's last member does not close the line.
    assert recover_top_level_id(head, b'xx","m":{"id":"rec-1"}}}\n') is None


@pytest.mark.parametrize("cut", [1, 3, 4])
def test_a_number_the_head_cuts_in_half_is_not_an_id(cut):
    """The head ending in ``"id":12`` of ``12345`` must not answer request 12."""
    line = b'{"jsonrpc":"2.0","method":"tools/call","params":{"s":"\xff"},"id":12345}'
    head = line[: line.index(b"12345") + cut]
    assert recover_top_level_id(head) is None
    assert recover_top_level_id(head, requests_only=True) is None
    assert recover_top_level_id(line, requests_only=True) == 12345


def test_a_number_straddling_the_probe_edge_falls_back_to_the_tail():
    params = b'{"s":"' + b"x" * 400 + b'\xff"}'
    line = b'{"jsonrpc":"2.0","method":"tools/call","params":' + params + b',"id":12345}'
    pad = json_line.ID_PROBE_BYTES - line.index(b"12345") - 2
    line = line.replace(b'"x', b'"' + b"x" * (pad + 1), 1)
    assert line[: json_line.ID_PROBE_BYTES].endswith(b'"id":12')
    assert recover_line_id(line) == 12345
    assert recover_line_id(line, requests_only=True) == 12345


def test_a_short_line_cut_before_its_close_never_yields_a_nested_id():
    """The head scan has seen all of a short line; the tail match assumes the
    line's real end and would read a nested object's last member there."""
    line = b'{"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info","data":{"id":"gw-9-3"}'
    assert len(line) <= json_line.ID_PROBE_BYTES
    assert recover_line_id(line) is None


@pytest.mark.parametrize(
    "line, expected",
    [
        (b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"s":"\xff"}}', 3),
        (b'{"method":"tools/call","params":{"s":"\xff"},"jsonrpc":"2.0","id":3}', 3),
        # A response carries its peer's own request id: never answered.
        (b'{"jsonrpc":"2.0","id":3,"result":{"s":"\xff"}}', None),
        (b'{"result":{"s":"\xff"},"jsonrpc":"2.0","id":3}', None),
        (b'{"jsonrpc":"2.0","id":3,"error":{"code":1,"message":"\xff"}}', None),
        # A nested method is someone else's.
        (b'{"jsonrpc":"2.0","id":3,"result":{"method":"x","s":"\xff"}}', None),
        # Past the head, the method is not seen: dropped, never misaddressed.
        (
            b'{"params":{"t":"' + b"x" * 2000 + b'\xff"},"method":"tools/call","id":3}',
            None,
        ),
    ],
    ids=[
        "request-id-first",
        "request-id-last",
        "response",
        "response-id-last",
        "error-response",
        "nested-method",
        "method-past-the-head",
    ],
)
def test_only_a_request_is_recovered_for_an_answer(line, expected):
    assert recover_line_id(line, requests_only=True) == expected


def test_recovery_reads_through_bytes_that_are_not_utf8():
    assert recover_top_level_id(b'{"id":"gw-10","error":{"message":"caf\xe9"}}') == "gw-10"


@pytest.mark.parametrize(
    "line, expected",
    [
        (b'{"jsonrpc":"2.0","id":"gw-11","result":{"t":"' + b"x" * 200_000 + b'\xff"}}\n', "gw-11"),
        # The MCP TypeScript SDK's order: the id closes the line.
        (b'{"result":{"t":"' + b"x" * 200_000 + b'\xff"},"jsonrpc":"2.0","id":"gw-12"}\n', "gw-12"),
        # In the middle of a long line: not probed, so the line is just dropped.
        (
            b'{"result":{"t":"' + b"x" * 200_000 + b'"},"id":"gw-13","pad":"' + b"y" * 600 + b'"}',
            None,
        ),
    ],
    ids=["id-first", "id-last", "id-in-the-middle"],
)
def test_the_line_probe_finds_an_id_at_either_end(line, expected):
    assert recover_line_id(line) == expected


def test_the_line_probe_reads_only_bounded_ends(monkeypatch):
    """The probe's cost must not grow with the line: a scan of a multi-MiB line
    would hold the shared pump, and through the GIL the event loop, for seconds."""
    seen: list[int] = []
    real = json_line._scan_top_level

    def _spy(text: str):
        seen.append(len(text))
        return real(text)

    monkeypatch.setattr(json_line, "_scan_top_level", _spy)
    line = b'{"result":"' + b"x" * (4 << 20) + b'\xff"}\n'
    assert recover_line_id(line) is None
    assert seen and max(seen) <= json_line.ID_PROBE_BYTES
