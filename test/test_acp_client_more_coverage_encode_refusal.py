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

    The depth is probed rather than hard-coded because the two encoders have
    different ceilings and both move between interpreter versions: ``indent=2``
    runs the pure-Python encoder, which spends several frames per level, and a
    ``default=str`` call runs the C one. A hard-coded depth can be encodable on
    another interpreter, and the test then passes having refused nothing.

    Every caller holds the cyclic collector off for the whole test, probe and
    call under test alike; see ``no_cyclic_gc_at_the_recursion_limit``.
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
    """Every backend-shaped frame the JSON codec refuses costs ONE frame's
    detail, never the agent turn and never a whole batch of tool results.

    ``RecursionError`` subclasses ``RuntimeError``, so an unguarded
    ``json.dumps`` does not catch it and neither does an
    ``except (TypeError, ValueError)`` arm or an ``except json.JSONDecodeError``
    arm. The agent backend chooses the shape of every ``rawInput`` /
    ``rawOutput`` these sites serialise, so a payload past the codec's ceiling is
    always reachable: unguarded, the raise escapes frame rendering and aborts the
    whole turn. Same refusal posture as the ``_dispatch`` encodes, and the
    placeholder is visible in the transcript on purpose, so the user can see that
    detail is missing.
    """

    @pytest.fixture
    def no_cyclic_gc_at_the_recursion_limit(self):
        """Keep the cyclic collector out of the frames next to the recursion limit.

        Every test in this class drives real code to ``RecursionError`` on
        purpose, so its innermost frames have no headroom left. A gen0 sweep that
        lands there -- the allocation counter decides where, not the test -- runs
        the finalizers of whatever cyclic garbage the worker is carrying. A
        pending Task leaked by another test reports itself through
        ``logger.error`` on ``__del__``; at that depth the report itself raises
        ``RecursionError``, the interpreter hands the escaped exception to
        ``sys.unraisablehook``, and pytest's hook overflows in the same place,
        surfacing as ``RuntimeError: Failed to process unraisable exception``
        against THIS test on any platform.

        Collect once at depth zero so inherited garbage pays its finalizers where
        there is stack for it, then hold the collector off for the whole test:
        the deep walk happens twice, once in the depth probe and once in the call
        under test, and only the second one is the product code. Reference
        counting still frees the walk's own dicts; cycles wait for teardown.
        Spelled as ``test_mcp_preflight`` spells it, per
        ``docs/system-specs/common/testing-conventions.md``.
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

        A ``use_aws``-shaped ``rawInput`` reaches ``AcpEvent.shell_command``,
        which the hook gate and the skill-read note both read while the turn is
        still running, and that property encodes ``parameters`` itself. Guarding
        only the extractor moves the raise one frame later instead of removing
        it, so the guarantee is pinned end to end here.

        The refusal denies rather than degrades: the parameters tail is the only
        place a smuggled command appears, so a synthesized command without it
        would be scanned as though it were complete. ``None`` routes the call to
        the unconditional deny-by-default arm in ``HookManager.on_tool_call``
        (``is_shell and not command``).
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

        The guard wraps the encode, so the ``[:4000]`` cut still applies to what
        the encode returns. Driven as a BATCH because that is where the cost of
        an unguarded refusal lands: it reaches the method's catch-all arm, which
        takes this result and every later one in the same read with it, while the
        file offset has already advanced past them.

        The bound is asserted as the behaviour that is there, not as the
        behaviour that is right: this reader takes its cut before anything
        redacts, which is its own defect class and its own change.
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
