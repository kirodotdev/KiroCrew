"""A frame or transcript line the JSON codec refuses costs its own detail, not the turn.

Tool-call, tool-result and refinement payloads past the encoder's recursion ceiling
render as ``UNSERIALISABLE_SIBLING_VALUE``, a call whose parameters cannot encode
fails closed, and the JSONL tool-result reader renders a refused result the same way
and loses only a line past the decoder's ceiling. The encode depth is probed on the
running interpreter. Shared helpers live in ``acp_client_more_coverage_helpers.py``.
"""

import gc
import json
from typing import Any

import pytest
from acp_client_more_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _client,
    _notify,
    _pinned_kiro_cli_version,
)
from stray_line_helpers import too_deep_to_decode as _too_deep_to_decode

import kiro_crew.acp.client as acp_client
from kiro_crew.acp._dispatch import UNSERIALISABLE_SIBLING_VALUE
from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE, EVENT_TOOL_RESULT

# ── Encoder and decoder refusal on the client's dispatch-path frames ──


def _too_deep_to_encode(**dumps_kwargs: Any) -> Any:
    """The shallowest nesting THIS interpreter's encoder refuses for *dumps_kwargs*.

    Probed, not hard-coded: ``indent=2`` (pure Python) and ``default=str`` (C) have
    different ceilings that move between versions. Callers hold the cyclic collector
    off; see ``no_cyclic_gc_at_the_recursion_limit``.
    """
    depth = 1000
    while depth <= 262144:
        payload: Any = {"leaf": "deep"}
        for _ in range(depth):
            payload = {"nested": payload}
        try:
            json.dumps(payload, **dumps_kwargs)
        except RecursionError:
            return payload
        depth *= 2
    raise AssertionError("no nesting up to 2**18 is refused by json.dumps")


def _nested_json_text(payload: Any) -> str:
    """Serialise a probe payload to JSON text WITHOUT ``json.dumps``.

    The JSONL sites need this: the encoder is what refuses the payload, so the
    fixture cannot use it to write the file the reader parses.
    """
    depth = 0
    node = payload
    while isinstance(node, dict) and "nested" in node:
        depth += 1
        node = node["nested"]
    return '{"nested": ' * depth + json.dumps(node) + "}" * depth


class TestClientEncodeRefusalDegrades:
    """A frame the JSON codec refuses costs that frame's detail, never the turn.

    ``RecursionError`` is a ``RuntimeError``, which ``(TypeError, ValueError)`` and
    ``json.JSONDecodeError`` arms miss, and the backend picks every ``rawInput`` /
    ``rawOutput`` shape. The placeholder is visible so the user sees detail is missing.
    """

    @pytest.fixture
    def no_cyclic_gc_at_the_recursion_limit(self):
        """Keep the cyclic collector out of the frames next to the recursion limit.

        A gen0 sweep at the recursion limit runs other tests' finalizers with no
        stack left, surfacing as ``RuntimeError: Failed to process unraisable
        exception``. So collect once at depth zero, then hold the collector off
        (as ``test_mcp_preflight`` does, per
        ``docs/system-specs/common/testing-conventions.md``).
        """
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            yield
        finally:
            if was_enabled:
                gc.enable()
            gc.collect()

    def test_tool_call_input_degrades(self, tmp_path, no_cyclic_gc_at_the_recursion_limit):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "deep-call",
                    "kind": "other",
                    "title": "Reading a pathological payload",
                    "rawInput": _too_deep_to_encode(indent=2),
                }
            },
        )

        event = client._extract_tool_event(msg)

        assert event is not None and event.kind == EVENT_TOOL_CALL
        assert event.tool_input == UNSERIALISABLE_SIBLING_VALUE

    def test_shell_command_of_a_refused_tool_call_fails_closed(
        self, tmp_path, no_cyclic_gc_at_the_recursion_limit
    ):
        """Rendering the frame is not the only encode the turn depends on.

        ``AcpEvent.shell_command`` encodes ``parameters`` itself, so this is pinned end
        to end. It denies rather than degrades: a smuggled command hides in the tail,
        and ``None`` hits ``HookManager.on_tool_call``'s ``is_shell and not command`` deny.
        """
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "deep-use-aws",
                    "kind": "execute",
                    "title": "Deleting an object",
                    "rawInput": {
                        "service_name": "s3api",
                        "operation_name": "delete-object",
                        "parameters": _too_deep_to_encode(sort_keys=True),
                    },
                }
            },
        )

        event = client._extract_tool_event(msg)

        assert event is not None and event.is_shell is True
        # The frame itself still renders, so the turn survives.
        assert event.tool_input == UNSERIALISABLE_SIBLING_VALUE
        # And the gate gets no command it could mistake for the whole call.
        assert event.shell_command is None
        # A parameters dict that encodes is unaffected: same frame, real bytes.
        shallow = dict(event.raw_tool_params or {})
        shallow["parameters"] = {"bucket": "b", "key": "k"}
        event.raw_tool_params = shallow
        assert event.shell_command == ('aws s3api delete-object {"bucket": "b", "key": "k"}')

    def test_json_envelope_output_degrades(self, tmp_path, no_cyclic_gc_at_the_recursion_limit):
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "deep-json",
                    "status": "completed",
                    "rawOutput": {"items": [{"Json": _too_deep_to_encode(default=str)}]},
                }
            },
        )

        event = client._extract_tool_call_update(msg)

        assert event is not None and event.kind == EVENT_TOOL_RESULT
        assert event.tool_output == UNSERIALISABLE_SIBLING_VALUE

    def test_raw_output_passthrough_degrades(self, tmp_path, no_cyclic_gc_at_the_recursion_limit):
        # No ``items`` key: the unstructured passthrough branch, which serialises
        # the whole ``rawOutput`` object.
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "deep-passthrough",
                    "status": "completed",
                    "rawOutput": _too_deep_to_encode(default=str),
                }
            },
        )

        event = client._extract_tool_call_update(msg)

        assert event is not None and event.kind == EVENT_TOOL_RESULT
        assert event.tool_output == UNSERIALISABLE_SIBLING_VALUE

    def test_refinement_input_degrades(self, tmp_path, no_cyclic_gc_at_the_recursion_limit):
        # The refinement input is the site whose own arm catches
        # ``(TypeError, ValueError)``, which this class of refusal walks straight
        # through, so the helper is what has to carry it.
        client = _client(tmp_path)
        msg = _notify(
            "session/update",
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "deep-refine",
                    "title": "grep",
                    "rawInput": _too_deep_to_encode(indent=2),
                }
            },
        )

        event = client._extract_tool_call_refinement(msg)

        assert event is not None and event.kind == EVENT_TOOL_CALL_UPDATE
        assert event.tool_input == UNSERIALISABLE_SIBLING_VALUE

    def test_jsonl_json_result_degrades(
        self, tmp_path, monkeypatch, no_cyclic_gc_at_the_recursion_limit
    ):
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        deep_text = _nested_json_text(_too_deep_to_encode(indent=2))
        # The decoder has to accept what the encoder refuses, or the fixture
        # never reaches the site under test.
        assert isinstance(json.loads(deep_text), dict)
        line = (
            '{"kind": "ToolResults", "data": {"content": [{"kind": "toolResult",'
            ' "data": {"toolUseId": "t-deep", "content": [{"kind": "json",'
            ' "data": ' + deep_text + "}]}}]}}\n"
        )
        (tmp_path / "sid.jsonl").write_text(line, encoding="utf-8", newline="\n")
        client = _client(tmp_path)
        client._session_id = "sid"

        results = client._read_new_tool_results_sync()

        assert [(r.tool_call_id, r.tool_output) for r in results] == [
            ("t-deep", UNSERIALISABLE_SIBLING_VALUE)
        ]

    def test_jsonl_batch_keeps_the_sibling_and_its_4000_char_bound(
        self, tmp_path, monkeypatch, no_cyclic_gc_at_the_recursion_limit
    ):
        """One refused frame costs that frame, and the per-part bound holds.

        A batch, because an unguarded refusal hits the catch-all and drops every later
        result after the offset advanced. The ``[:4000]`` cut is pinned as it is: it
        runs before redaction, a separate defect.
        """
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        wide = {f"key{i:04d}": "v" * 32 for i in range(200)}
        assert len(json.dumps(wide, indent=2)) > 4000
        deep_text = _nested_json_text(_too_deep_to_encode(indent=2))
        line = (
            '{"kind": "ToolResults", "data": {"content": ['
            '{"kind": "toolResult", "data": {"toolUseId": "t-deep",'
            ' "content": [{"kind": "json", "data": ' + deep_text + "}]}},"
            '{"kind": "toolResult", "data": {"toolUseId": "t-wide",'
            ' "content": [{"kind": "json", "data": ' + json.dumps(wide) + "}]}}"
            "]}}\n"
        )
        (tmp_path / "sid.jsonl").write_text(line, encoding="utf-8", newline="\n")
        client = _client(tmp_path)
        client._session_id = "sid"

        results = client._read_new_tool_results_sync()

        assert [r.tool_call_id for r in results] == ["t-deep", "t-wide"]
        assert results[0].tool_output == UNSERIALISABLE_SIBLING_VALUE
        assert results[1].tool_output == json.dumps(wide, indent=2)[:4000]
        assert len(results[1].tool_output) == 4000

    def test_jsonl_undecodable_line_costs_only_that_line(
        self, tmp_path, monkeypatch, no_cyclic_gc_at_the_recursion_limit
    ):
        """The decode half of the same read carries the same refusal class.

        ``json.loads`` is guarded by ``json.JSONDecodeError``, a ``ValueError``,
        which this class is not: a line past the decoder's ceiling that reaches
        the method's catch-all arm takes every LATER line's results with it,
        because the saved file offset has already moved past the bad line.
        """
        monkeypatch.setattr(acp_client, "kiro_sessions_dir", lambda: tmp_path)
        good = json.dumps(
            {
                "kind": "ToolResults",
                "data": {
                    "content": [
                        {
                            "kind": "toolResult",
                            "data": {
                                "toolUseId": "t-after",
                                "content": [{"kind": "text", "data": "SURVIVES"}],
                            },
                        }
                    ]
                },
            }
        )
        body = _too_deep_to_decode() + "\n" + good + "\n"
        (tmp_path / "sid.jsonl").write_text(body, encoding="utf-8", newline="\n")
        client = _client(tmp_path)
        client._session_id = "sid"

        results = client._read_new_tool_results_sync()

        assert [(r.tool_call_id, r.tool_output) for r in results] == [("t-after", "SURVIVES")]
