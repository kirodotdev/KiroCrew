"""``initialize`` and ``tools/list`` have one builder each, used before and after the handlers load.

``kiro_crew.mcp_shared_listing`` owns both answers. The dispatch loop answers from them,
and so does ``serve_before_handlers``, which a server's entry calls before it imports its
tool handlers. These tests pin that the two callers say the same thing, that the early
answers take only the two requests they serve, and that every frame they leave is handled
by the loop exactly once and in the order it arrived.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from typing import Any

import pytest
from mcp_shared_helpers import _framing_undetected, _tools_call  # noqa: F401 -- autouse fixture

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import mcp_shared_listing as listing
from kiro_crew.mcp_caller import INFLIGHT_CAPABILITY_KEY, caller_identity_capability
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
_TOOLS = [{"name": "alpha"}, {"name": "beta"}]


class _Client:
    """A pipe-backed stdin, and every answer written to it, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read_fd, self._write_fd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(read_fd, "rb"))
        self.answers: list[tuple[Any, Any, Any]] = []
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(mcp_shared, "snapshot_stdout_fd", lambda: None)
        monkeypatch.setattr(mcp_shared, "_EARLY", listing._EarlyState())
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), ""),
        )
        monkeypatch.setattr(mcp_shared, "serves_managed_tools", lambda _fn: True)

    def _record(self, req_id: Any, result: Any, error: Any = None) -> bool:
        self.answers.append((req_id, result, error))
        return True

    def send(self, *frames: dict[str, Any]) -> None:
        for frame in frames:
            os.write(self._write_fd, (json.dumps(frame) + "\n").encode("utf-8"))

    def hang_up(self) -> None:
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1

    def close(self) -> None:
        self.hang_up()
        self._stdin.close()

    def answered(self, req_id: Any) -> list[Any]:
        return [result for rid, result, _error in self.answers if rid == req_id]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    made = _Client(monkeypatch)
    yield made
    made.close()


def _server(**overrides: Any) -> listing.StdioServer:
    values: dict[str, Any] = {
        "name": "kirocrew-test",
        "version": "1.2.3",
        "list_tools": lambda: _TOOLS,
    }
    values.update(overrides)
    return listing.StdioServer(**values)


# ── the two builders ────────────────────────────────────────────────────────


def test_a_plain_server_initializes_with_tools_and_its_name() -> None:
    assert listing.initialize_result("kirocrew-test", "1.2.3") == {
        "protocolVersion": listing.PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "kirocrew-test", "version": "1.2.3"},
    }


def test_each_option_adds_only_its_own_part_of_the_initialize_result() -> None:
    result = listing.initialize_result(
        "s", "0", advertise_caller_identity=True, reports_inflight=True, instructions="read me"
    )
    assert result["instructions"] == "read me"
    assert result["capabilities"]["experimental"] == {
        **caller_identity_capability(),
        INFLIGHT_CAPABILITY_KEY: {},
    }
    only_identity = listing.initialize_result("s", "0", advertise_caller_identity=True)
    assert only_identity["capabilities"]["experimental"] == caller_identity_capability()
    assert "instructions" not in only_identity


def test_a_server_passes_the_loop_only_the_options_it_sets() -> None:
    assert _server().loop_options() == {}
    declared = _server(advertise_caller_identity=True, reports_inflight=True, instructions="i")
    assert declared.loop_options() == {
        "advertise_caller_identity": True,
        "reports_inflight": True,
        "instructions": "i",
    }
    # The loop takes no ``before_first_request``: the server's own ``run`` owns that step.
    assert "before_first_request" not in _server(before_first_request=lambda: None).loop_options()


def test_the_list_drops_what_the_session_excludes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_shared,
        "_resolve_tool_policy",
        lambda *a, **k: mcp_shared.ToolPolicy(frozenset({"beta"}), ""),
    )
    listed = listing.listable_tools("s", lambda: list(_TOOLS), None, applies_policy=True)
    assert [tool["name"] for tool in listed] == ["alpha"]


def test_an_unreadable_policy_lists_everything_and_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audited: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **fields: Any) -> None:
            audited.append(fields)

    monkeypatch.setattr(
        mcp_shared,
        "_resolve_tool_policy",
        lambda *a, **k: mcp_shared.ToolPolicy(frozenset({"beta"}), "gateway unreachable"),
    )
    monkeypatch.setattr(mcp_shared, "sel", lambda: _Sel())
    monkeypatch.setattr(mcp_shared, "_ambient_audit_session", lambda: "dashboard:own")
    listed = listing.listable_tools("s", lambda: list(_TOOLS), None, applies_policy=True)
    assert [tool["name"] for tool in listed] == ["alpha", "beta"]
    assert [(a["operation"], a["outcome"], a["caller"]) for a in audited] == [
        ("tool_policy.unfiltered_listing", "unresolved", "dashboard:own")
    ]


def test_a_server_with_no_managed_tools_reads_no_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the policy was read for a server it does not apply to")

    monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", _never)
    assert listing.caller_tool_policy(None, applies_policy=False) == mcp_shared.ToolPolicy(
        frozenset(), ""
    )


# ── the early answers ───────────────────────────────────────────────────────


def test_the_two_requests_are_answered_before_the_handlers_load(client: _Client) -> None:
    client.send(_INITIALIZE, _INITIALIZED, _LIST)
    server = _server(advertise_caller_identity=True, instructions="read me")
    listing.serve_before_handlers(server, wait_secs=FALLBACK_TIMEOUT_SECS)
    assert client.answers == [
        (
            1,
            listing.initialize_result(
                "kirocrew-test", "1.2.3", advertise_caller_identity=True, instructions="read me"
            ),
            None,
        ),
        (2, {"tools": _TOOLS}, None),
    ]
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == []
    assert frames.init_params == _INITIALIZE["params"]


def test_the_frames_are_handed_to_one_loop_only(client: _Client) -> None:
    client.send(_INITIALIZE, _LIST)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    assert listing.take_early_frames() is not None
    assert listing.take_early_frames() is None


def test_a_second_call_answers_nothing(client: _Client) -> None:
    client.send(_INITIALIZE, _LIST)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    client.send(_INITIALIZE, _LIST)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    assert [rid for rid, _result, _error in client.answers] == [1, 2]


def test_a_call_that_arrives_first_is_left_for_the_loop(client: _Client) -> None:
    call = _tools_call(7, "alpha")
    client.send(_INITIALIZE, call, _LIST)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    assert [rid for rid, _result, _error in client.answers] == [1]
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == [call]


def test_a_first_frame_that_is_not_initialize_is_left_unanswered(client: _Client) -> None:
    client.send(_LIST)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    assert client.answers == []
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == [_LIST] and frames.init_params is None


def test_a_malformed_frame_is_left_for_the_loop_to_refuse(client: _Client) -> None:
    bad = {"jsonrpc": "1.0", "id": 5, "method": "initialize"}
    client.send(bad)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    assert client.answers == []
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == [bad]


def test_a_silent_client_does_not_hold_the_handlers_back(
    client: _Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait for the client's next frame is bounded, and a wait that ends with no
    frame ends the early answers. The read is scripted, so no time passes here."""
    waits: list[float | None] = []
    script = iter([_INITIALIZE, mcp_shared.NO_FRAME])

    def _next(_stdin: Any, _pump: Any, timeout: float | None, _wake: Any = None) -> Any:
        waits.append(timeout)
        return next(script)

    monkeypatch.setattr(mcp_shared, "_frame_within", _next)
    listing.serve_before_handlers(_server())
    assert [rid for rid, _result, _error in client.answers] == [1]
    assert waits == [listing.EARLY_FRAME_WAIT_SECS] * 2
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == []


def test_the_end_of_the_stream_is_handed_to_the_loop(client: _Client) -> None:
    client.send(_INITIALIZE)
    client.hang_up()
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == [None]


def test_a_list_that_cannot_be_built_is_left_for_the_loop(client: _Client) -> None:
    def _broken() -> list[dict[str, Any]]:
        raise RuntimeError("catalogue unreadable")

    client.send(_INITIALIZE, _LIST)
    listing.serve_before_handlers(_server(list_tools=_broken), wait_secs=FALLBACK_TIMEOUT_SECS)
    assert [rid for rid, _result, _error in client.answers] == [1]
    frames = listing.take_early_frames()
    assert frames is not None and list(frames.pending) == [_LIST]


def test_the_step_a_server_owes_runs_before_its_first_read(client: _Client) -> None:
    order: list[str] = []
    client.send(_INITIALIZE, _LIST)
    server = _server(
        before_first_request=lambda: order.append("step"),
        list_tools=lambda: order.append("list") or _TOOLS,
    )
    listing.serve_before_handlers(server, wait_secs=FALLBACK_TIMEOUT_SECS)
    assert order == ["step", "list"]


# ── the loop that follows ───────────────────────────────────────────────────


def _run_loop(client: _Client, call_tool: Any, **options: Any) -> threading.Thread:
    thread = threading.Thread(
        target=mcp_shared.run_mcp_stdio_loop,
        args=("kirocrew-test", "1.2.3", lambda: _TOOLS, call_tool),
        kwargs=options,
        daemon=True,
    )
    thread.start()
    return thread


def test_the_loop_answers_what_was_left_once_and_in_order(client: _Client) -> None:
    first, second = _tools_call(7, "alpha"), _tools_call(8, "beta")
    client.send(_INITIALIZE, first)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    client.send(second)
    loop = _run_loop(client, lambda name, _args: f"ran {name}")
    try:
        wait_until(lambda: len(client.answered(8)) == 1, describe=lambda: repr(client.answers))
        assert [rid for rid, _result, _error in client.answers] == [1, 7, 8]
        assert client.answered(7)[0]["content"][0]["text"] == "ran alpha"
    finally:
        client.hang_up()
        loop.join(timeout=FALLBACK_TIMEOUT_SECS)
    assert not loop.is_alive()


def test_a_later_list_is_built_by_the_same_function(client: _Client) -> None:
    client.send(_INITIALIZE, _LIST)
    listing.serve_before_handlers(_server(), wait_secs=FALLBACK_TIMEOUT_SECS)
    client.send({**_LIST, "id": 3})
    loop = _run_loop(client, lambda name, _args: name)
    try:
        wait_until(lambda: len(client.answered(3)) == 1, describe=lambda: repr(client.answers))
    finally:
        client.hang_up()
        loop.join(timeout=FALLBACK_TIMEOUT_SECS)
    assert client.answered(3) == client.answered(2) == [{"tools": _TOOLS}]


def test_a_loop_with_no_early_answers_initializes_itself(client: _Client) -> None:
    client.send(_INITIALIZE, _LIST)
    loop = _run_loop(client, lambda name, _args: name, instructions="read me")
    try:
        wait_until(lambda: len(client.answered(2)) == 1, describe=lambda: repr(client.answers))
    finally:
        client.hang_up()
        loop.join(timeout=FALLBACK_TIMEOUT_SECS)
    assert client.answers == [
        (1, listing.initialize_result("kirocrew-test", "1.2.3", instructions="read me"), None),
        (2, {"tools": _TOOLS}, None),
    ]
