"""Connected computers B14: a tool turned off locally stays off when its server is shared.

The local machine's ``disabledTools`` for a ticked stdio server is snapshotted
into the grant and enforced ON THE LOCAL MACHINE by the connector, so a remote
gateway that sends a call for that tool anyway gets an error and the server
never sees it. The server's other tools stay usable: sharing is not met by
refusing the server.

Authored for CI; this host runs no tests locally.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.mcp_filter import MAX_DISABLED_TOOLS, MAX_LINE_BYTES, ToolFilter
from kiro_crew.testing.ids import UNALLOCATABLE_PID

pytestmark = pytest.mark.asyncio


def _line(obj: Any) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def _call(id_: Any, name: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": {"name": name}}


def _lines(data: bytes) -> list[Any]:
    return [json.loads(x) for x in data.splitlines() if x]


# -- inbound: gateway -> local server ---------------------------------------------


def test_b14_a_call_to_a_tool_turned_off_locally_never_reaches_the_server():
    f = ToolFilter(["rm"])
    to_child, replies = f.inbound(_line(_call(7, "rm")))
    assert to_child == b""
    [reply] = _lines(replies)
    assert reply["id"] == 7
    assert reply["result"]["isError"] is True
    assert "turned off on this computer" in reply["result"]["content"][0]["text"]


def test_b14_a_call_to_an_allowed_tool_is_forwarded():
    f = ToolFilter(["rm"])
    to_child, replies = f.inbound(_line(_call(1, "ls")))
    assert _lines(to_child) == [_call(1, "ls")]
    assert replies == b""


def test_b14_a_duplicate_name_key_cannot_smuggle_a_turned_off_tool():
    # Python keeps the last key; another parser might keep the first. The server
    # receives the re-encoded message, which holds only the name that was checked.
    raw = b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"rm","name":"ls"}}\n'
    to_child, _ = ToolFilter(["rm"]).inbound(raw)
    assert to_child.count(b'"name"') == 1
    assert _lines(to_child)[0]["params"]["name"] == "ls"
    raw = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ls","name":"rm"}}\n'
    to_child, replies = ToolFilter(["rm"]).inbound(raw)
    assert to_child == b"" and _lines(replies)[0]["id"] == 3


def test_b14_a_line_that_is_not_json_is_dropped_not_forwarded():
    to_child, replies = ToolFilter(["rm"]).inbound(b"not json\n" + b"[1,NaN]\n")
    assert to_child == b"" and replies == b""


def test_b14_a_call_split_across_chunks_is_judged_whole():
    f = ToolFilter(["rm"])
    data = _line(_call(4, "rm"))
    out_a, rep_a = f.inbound(data[:10])
    out_b, rep_b = f.inbound(data[10:])
    assert out_a == out_b == b""
    assert rep_a == b"" and _lines(rep_b)[0]["id"] == 4


def test_b14_a_batch_keeps_allowed_calls_and_answers_the_turned_off_one():
    f = ToolFilter(["rm"])
    to_child, replies = f.inbound(_line([_call(1, "ls"), _call(2, "rm")]))
    assert _lines(to_child) == [[_call(1, "ls")]]
    assert [r["id"] for r in _lines(replies)] == [2]


def test_b14_a_batch_of_only_turned_off_calls_sends_nothing():
    to_child, replies = ToolFilter(["rm"]).inbound(_line([_call(1, "rm"), _call(2, "rm")]))
    assert to_child == b""
    assert [r["id"] for r in _lines(replies)] == [1, 2]


def test_b14_a_turned_off_notification_call_is_dropped_without_a_reply():
    note = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "rm"}}
    to_child, replies = ToolFilter(["rm"]).inbound(_line(note))
    assert to_child == b"" and replies == b""


def test_b14_an_over_long_line_is_dropped_whole():
    f = ToolFilter(["rm"])
    head = b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"rm","pad":"'
    to_child, _ = f.inbound(head + b"x" * (MAX_LINE_BYTES + 1))
    assert to_child == b""
    # The rest of that line is discarded too; the next line is judged normally.
    to_child, _ = f.inbound(b'"}}\n' + _line(_call(10, "ls")))
    assert _lines(to_child) == [_call(10, "ls")]


def test_b14_a_last_line_without_newline_is_judged_at_eof():
    f = ToolFilter(["rm"])
    assert f.inbound(json.dumps(_call(5, "rm")).encode()) == (b"", b"")
    to_child, replies = f.inbound_eof()
    assert to_child == b"" and _lines(replies)[0]["id"] == 5
    f = ToolFilter(["rm"])
    f.inbound(json.dumps(_call(6, "ls")).encode())
    to_child, _ = f.inbound_eof()
    assert _lines(to_child) == [_call(6, "ls")]


# -- outbound: local server -> gateway --------------------------------------------


def _list_reply(id_: Any, names: list[str]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [{"name": n} for n in names]}}


def test_b14_the_tool_list_hides_tools_turned_off_locally():
    f = ToolFilter(["rm"])
    f.inbound(_line({"jsonrpc": "2.0", "id": "L1", "method": "tools/list"}))
    out = f.outbound(_line(_list_reply("L1", ["ls", "rm", "cat"])))
    assert [t["name"] for t in _lines(out)[0]["result"]["tools"]] == ["ls", "cat"]


def test_b14_other_replies_pass_byte_for_byte():
    f = ToolFilter(["rm"])
    raw = b'{"jsonrpc":"2.0", "id":1,   "result":{"tools":[{"name":"rm"}]}}\n'
    assert f.outbound(raw) == raw, "not a reply to a tools/list this filter saw"


def test_b14_output_is_sent_only_in_whole_lines():
    f = ToolFilter(["rm"])
    assert f.outbound(b'{"a":') == b""
    assert f.at_boundary
    assert f.outbound(b"1}\n") == b'{"a":1}\n'


def test_b14_an_over_long_output_line_passes_through_and_marks_mid_line():
    f = ToolFilter(["rm"])
    big = b"y" * (MAX_LINE_BYTES + 1)
    assert f.outbound(big) == big
    assert not f.at_boundary
    assert f.outbound(b"z\n") == b"z\n"
    assert f.at_boundary


def test_b14_tracked_lists_are_bounded_and_one_past_is_refused():
    f = ToolFilter(["rm"])
    for i in range(64):
        to_child, _ = f.inbound(_line({"jsonrpc": "2.0", "id": i, "method": "tools/list"}))
        assert to_child
    to_child, replies = f.inbound(_line({"jsonrpc": "2.0", "id": 99, "method": "tools/list"}))
    assert to_child == b""
    assert _lines(replies)[0]["error"]["code"] == -32000


def test_b14_the_turned_off_list_itself_is_bounded():
    with pytest.raises(ValueError):
        ToolFilter([f"t{i}" for i in range(MAX_DISABLED_TOOLS + 1)])


# -- globs: an entry is read as kiro-cli reads it ----------------------------------


def test_b14_a_glob_entry_refuses_a_call_to_every_tool_it_covers():
    f = ToolFilter(["delete_*", "drop?"])
    for name in ("delete_repo", "delete_", "drop1"):
        to_child, replies = f.inbound(_line(_call(name, name)))
        assert to_child == b"", name
        assert _lines(replies)[0]["result"]["isError"] is True
    to_child, replies = f.inbound(_line(_call(9, "deleted")))
    assert _lines(to_child) == [_call(9, "deleted")]
    assert replies == b""


def test_b14_a_glob_entry_hides_the_tools_it_covers_from_the_tool_list():
    f = ToolFilter(["delete_*"])
    f.inbound(_line({"jsonrpc": "2.0", "id": "L", "method": "tools/list"}))
    out = f.outbound(_line(_list_reply("L", ["ls", "delete_repo", "Delete_repo"])))
    assert [t["name"] for t in _lines(out)[0]["result"]["tools"]] == ["ls", "Delete_repo"]


@pytest.mark.parametrize("entry", ["{a,b}*", "a\\*", "x}?"])
def test_b14_a_glob_fnmatch_cannot_read_covers_every_tool(entry):
    f = ToolFilter([entry])
    to_child, replies = f.inbound(_line(_call(1, "ls")))
    assert to_child == b""
    assert _lines(replies)[0]["result"]["isError"] is True
    f.inbound(_line({"jsonrpc": "2.0", "id": "L", "method": "tools/list"}))
    out = f.outbound(_line(_list_reply("L", ["ls", "cat"])))
    assert _lines(out)[0]["result"]["tools"] == []


def test_b14_the_connector_reads_disabled_tools_as_the_cron_path_does():
    from kiro_crew.connected_computers.mcp_filter import tool_is_turned_off
    from kiro_crew.cron_script_mcp import _tool_is_switched_off

    entries = ["rm", "delete_*", "get?", "{a,b}*", "a\\*", "[ab]x", "*"]
    tools = ["rm", "rmx", "delete_repo", "get1", "get", "ax", "bx", "cx", "", "ls"]
    for one in entries:
        for tool in tools:
            want = _tool_is_switched_off(tool, frozenset([one]))
            assert tool_is_turned_off(tool, [one]) is want, (one, tool)


# -- snapshot, storage and the connector ------------------------------------------


def test_b14_a_server_with_tools_turned_off_is_shared_with_them_kept_off():
    from kiro_crew.connected_computers.local_mcp import exposable, snapshot
    from kiro_crew.mcp_discovery_config import McpServerInfo

    limited = McpServerInfo(name="limited", command="/usr/bin/x", disabled_tools=["rm", "rm"])
    [exposed] = snapshot(["limited"], [limited])
    assert exposed.disabled_tools == ("rm",)
    assert exposable([limited])[0]["refused"] == ""


def test_b14_an_unreadable_turned_off_list_is_refused():
    from types import SimpleNamespace

    from kiro_crew.connected_computers.local_mcp import McpExposureRefused, snapshot

    odd = SimpleNamespace(name="odd", command="/usr/bin/x", disabled_tools="rm", env={})
    with pytest.raises(McpExposureRefused, match="odd"):
        snapshot(["odd"], [odd])


def test_b14_the_turned_off_list_survives_storage_and_an_old_row_reads_empty():
    from test_connected_computers_local import _grant as make_grant

    from kiro_crew.connected_computers.local_store import ExposedMcp, _grant, _row

    grant = make_grant(mcp=(ExposedMcp(name="n", command="/x", disabled_tools=("rm",)),))
    row = _row(grant, {})
    assert row["mcp"][0]["disabled_tools"] == ["rm"]
    assert _grant(row).mcp[0].disabled_tools == ("rm",)  # type: ignore[union-attr]
    del row["mcp"][0]["disabled_tools"]
    assert _grant(row).mcp[0].disabled_tools == ()  # type: ignore[union-attr]


class _Stdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Sink:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text)["frame"])


def _channel_with_filter(disabled: list[str]):
    from types import SimpleNamespace

    from kiro_crew.connected_computers.authenticated import SessionFrames
    from kiro_crew.connected_computers.connector import Connector, _LocalChannel
    from kiro_crew.connected_computers.local_store import Grant

    grant = Grant(
        grant_id="g1",
        instance_id="inst-1",
        gateway_id="gw-1",
        gateway_label="remote",
        device_id="laptop",
        root="/work",
    )
    connector = Connector(
        grant, url="ws://127.0.0.1:1/x", plan=None, grant_still_active=lambda: True
    )
    sink = _Sink()
    connector._ws = sink
    connector._frames = SessionFrames(
        "ab" * 32, ("gw-1", "laptop", "0" * 64, "1" * 64), gateway=False
    )
    proc = SimpleNamespace(stdin=_Stdin(), stdout=None, returncode=None, pid=UNALLOCATABLE_PID)
    channel = _LocalChannel(3, proc, "/nonexistent", tool_filter=ToolFilter(disabled))  # type: ignore[arg-type]
    return connector, channel, sink, proc


async def test_b14_the_connector_answers_a_turned_off_call_and_never_writes_it():
    connector, channel, sink, proc = _channel_with_filter(["rm"])
    data = _line(_call(8, "rm")) + _line(_call(9, "ls"))
    channel.inbound.feed(data)
    channel.inbound.feed_eof()
    await asyncio.wait_for(connector._pump_in(channel), 30)

    assert _lines(proc.stdin.written) == [_call(9, "ls")]
    assert proc.stdin.closed
    replies = b"".join(protocol.data_of(f) for f in sink.sent if f["op"] == "data")
    assert [r["id"] for r in _lines(replies)] == [8]
    # Credit covers every byte the gateway sent, forwarded or not.
    assert sum(f["bytes"] for f in sink.sent if f["op"] == "credit") == len(data)


async def test_b14_without_turned_off_tools_the_stream_is_byte_for_byte():
    from types import SimpleNamespace

    from kiro_crew.connected_computers.connector import _LocalChannel

    connector, _c, sink, _p = _channel_with_filter(["rm"])
    proc = SimpleNamespace(stdin=_Stdin(), stdout=None, returncode=None, pid=UNALLOCATABLE_PID)
    channel = _LocalChannel(4, proc, "/nonexistent")  # type: ignore[arg-type]
    raw = b'not json at all\n{ "id" : 1 }'
    channel.inbound.feed(raw)
    channel.inbound.feed_eof()
    await asyncio.wait_for(connector._pump_in(channel), 30)
    assert proc.stdin.written == raw
    assert not [f for f in sink.sent if f["op"] == "data"]


def test_b14_the_connector_filters_only_a_server_with_tools_turned_off():
    from test_connected_computers_local import _grant as make_grant

    from kiro_crew.connected_computers.connector import Connector
    from kiro_crew.connected_computers.local_store import ExposedMcp

    grant = make_grant(
        mcp=(
            ExposedMcp(name="limited", command="/x", disabled_tools=("rm",)),
            ExposedMcp(name="plain", command="/y"),
        )
    )
    connector = Connector(
        grant, url="ws://127.0.0.1:1/x", plan=None, grant_still_active=lambda: True
    )
    assert connector._filter_for("mcp:limited") is not None
    assert connector._filter_for("mcp:plain") is None
    assert connector._filter_for(protocol.KIND_WORKSPACE) is None


class _CreditingSink(_Sink):
    """The gateway side: grants back the credit for each data frame it receives."""

    def __init__(self, channel) -> None:
        super().__init__()
        self.channel = channel

    async def send_str(self, text: str) -> None:
        await super().send_str(text)
        frame = self.sent[-1]
        if frame["op"] == "data":
            self.channel.window.grant(len(protocol.data_of(frame)))


@pytest.mark.parametrize("send", ["emit", "reply"])
async def test_b14_a_filtered_message_larger_than_the_window_is_still_sent(send):
    """A filtered server buffers a whole line (up to 8 MiB), past the 1 MiB window."""
    connector, channel, _sink, _proc = _channel_with_filter(["rm"])
    sink = _CreditingSink(channel)
    connector._ws = sink
    data = b"x" * (protocol.WINDOW_BYTES * 2 + 17) + b"\n"
    if send == "emit":
        await asyncio.wait_for(connector._emit(channel, data), 30)
    else:
        await asyncio.wait_for(connector._reply(channel, data), 30)
    frames = [f for f in sink.sent if f["op"] == "data"]
    assert b"".join(protocol.data_of(f) for f in frames) == data
    assert all(len(protocol.data_of(f)) <= protocol.MAX_DATA_BYTES for f in frames)
