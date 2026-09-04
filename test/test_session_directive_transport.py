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
* the same parser again, for an envelope it does NOT recognise as a text
  envelope, or a result the backend hands back ALREADY serialised (observed on
  KAS). Both reach ``json.dumps`` too, and the escaping leaves the quote-free
  sentinel intact while destroying the payload behind it — so the frame still
  looks like it carries a directive and names no parked record. The last class
  here is a ratchet over every ``EVENT_TOOL_RESULT`` builder, because the person
  who adds the next one is a new provider's author, who will not know this
  constraint exists.
"""

import ast
import json
import types

from source_corpus import parsed_candidates, src_root

from kiro_crew import session_directive as sd
from kiro_crew.acp._dispatch import (
    _build_tool_result_event,
    _mcp_content_text,
    _repair_escaped_marker,
    parse_session_update,
)
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import EVENT_TOOL_RESULT, JsonRpcMessage
from kiro_crew.mcp_apps_render import find_marker
from kiro_crew.mcp_gateway.apps import append_marker
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


class TestRefusalMarkerSurvivesTransport:
    """The refusal marker rides the SAME sanitizer + parser path as the directive
    marker, so if it does not survive, the consumer cannot tell a by-design
    oversize refusal from a marker lost in transport and logs every refusal as a
    suspected escaping bug."""

    def _refusal(self) -> str:
        huge = "x" * (sd.MAX_DIRECTIVE_CHARS + 500)
        return sd.encode("ask_question", {"questions": [{"question": huge}]}, "asked")

    def test_refusal_marker_is_pure_ascii_and_survives_the_sanitizer(self):
        # The prose carries an em dash, but the framing TOKEN must stay ASCII —
        # the sanitizer strips category Cf, which is what destroyed an earlier
        # invisible-separator prefix on the directive marker.
        assert sd._REFUSAL_SENTINEL.isascii()
        refusal = self._refusal()
        assert strip_hidden_unicode(refusal) == refusal
        text = build_tool_response(refusal)["content"][0]["text"]
        assert sd.is_refusal(text)
        assert sd.decode(text, "ask_question") is None

    def test_refusal_survives_raw_output_json_envelope(self):
        update = {
            "toolCallId": "tc-refusal",
            "status": "completed",
            "rawOutput": {"items": [{"Json": _mcp_envelope(self._refusal())}]},
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert event.tool_final is True
        assert sd.is_refusal(event.tool_output)


class TestMcpAppMarkerSurvivesResultCuts:
    """The MCP App render marker must survive both truncation cuts in
    ``_build_tool_result_event`` — the per-part 4000-char cut and the 8000-char
    join cut — or ``mcp_apps_render.find_marker`` never sees it and the app
    never mounts (issue #6606). The gateway prepends the marker at offset 0 of
    the first text block, and the parser re-injects it after the join cut."""

    def _marker(self) -> str:
        # A valid marker carries a 32-lowercase-hex spool id.
        return "[kirocrew-mcp-app:" + "a" * 32 + "]"

    def _id(self) -> str:
        return "a" * 32

    def test_marker_survives_long_single_block(self):
        # Drive the marker through the real producer ``append_marker`` on a
        # LONG (>4000-char) first block, then feed the marked envelope through
        # the parser. The producer decides the marker's byte offset, so this
        # regresses the fix: with the prepend it sits at offset 0 and rides the
        # per-part 4000-char cut, but the old end-append put it past 20000 chars
        # where the ``[:4000]`` slice drops it and ``find_marker`` returns None.
        marked = append_marker({"content": [{"type": "text", "text": "x" * 20000}]}, self._id())
        update = {
            "toolCallId": "tc-long",
            "status": "completed",
            "rawOutput": {"items": [{"Json": marked}]},
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert find_marker(event.tool_output) == self._id()

    def test_marker_survives_multi_part_join_cut(self):
        # Two prior ~4000-char parts push the marker part's offset-0 marker
        # past the 8000-char join cut; the parser must re-inject it so it stays
        # detectable.
        update = {
            "toolCallId": "tc-multi",
            "status": "completed",
            "rawOutput": {
                "items": [
                    {"Text": "a" * 4000},
                    {"Text": "b" * 4000},
                    {"Json": _mcp_envelope(self._marker() + " drawn")},
                ]
            },
        }
        event = _build_tool_result_event(update)
        assert event is not None
        assert find_marker(event.tool_output) == self._id()


# A quote INSIDE the directive's own text is the point of this fixture, not
# decoration: encode escapes it once and the envelope's dump escapes it again, so
# a recovery that merely replaces ``\"`` with ``"`` collapses it into a dangling
# backslash-quote that ends the JSON string early. Every real monitor_start
# message quotes its stop reason, so a fixture without a quote would pass against
# a repair that cannot handle a single actual directive.
MONITOR_ARGS = {
    "message": 'Report the cycle. After cycle 3 call autonudge_stop with reason "done".',
    "idle_secs": 60,
    "max_cycles": 3,
    "max_runtime_secs": 0,
}


def _monitor() -> str:
    text = sd.encode("monitor_start", MONITOR_ARGS, "Armed: fires every 60s, 3 cycles.")
    assert sd.peek(text) is not None, "fixture must start readable"
    return text


def _pre_serialised(text: str) -> str:
    """The result as a backend hands it back: inside a serialised envelope."""
    dumped = json.dumps({"stdout": text})
    assert sd.has_marker(dumped), "the sentinel survives the dump"
    assert sd.peek(dumped) is None, "but the selector does not"
    return dumped


def _update(**extra: object) -> dict[str, object]:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc-1",
        "status": "completed",
        **extra,
    }


def _runtime_output(update: dict[str, object]) -> str | None:
    """Drive the live consumer path: AcpRuntime -> handle -> parse_session_update."""
    results = [
        e for e in parse_session_update(update, cache_scope="scope") if e.kind == EVENT_TOOL_RESULT
    ]
    return results[0].tool_output if results else None


class TestSurvivesUnrecognisedResultEnvelope:
    """Defect 3: an envelope with no recognised text field must not be dumped
    over a directive. The recovery keys on the SENTINEL, not on a field name,
    because the field differs per backend."""

    def test_directive_survives_any_envelope_key(self):
        for key in ("output", "Ok", "result", "content"):
            out = _runtime_output(_update(rawOutput={"items": [{"Json": {key: _monitor()}}]}))
            assert sd.peek(out) == ("monitor_start", MONITOR_ARGS), key

    def test_marker_free_envelope_is_still_serialised(self):
        payload = {"exit_status": 0, "note": "hi"}
        out = _runtime_output(_update(rawOutput={"items": [{"Json": payload}]}))
        assert out == json.dumps(payload, default=str)

    def test_two_competing_directives_are_not_guessed_between(self):
        # Applying the WRONG directive is worse than applying none, so a frame
        # naming two DIFFERENT directives must degrade rather than pick one --
        # including at the join-point recovery, which reads the first marker line.
        other = sd.encode("monitor_start", {**MONITOR_ARGS, "idle_secs": 900}, "Armed: every 900s.")
        out = _runtime_output(
            _update(rawOutput={"items": [{"Json": {"a": _monitor(), "b": other}}]})
        )
        assert sd.peek(out) is None
        assert out is not None and out.startswith("{")


class TestSurvivesPreSerialisedResultText:
    """Defect 3b: the backend hands the result back already JSON-encoded, so the
    text this parser receives is the DUMP of an envelope rather than the
    envelope. Observed on KAS as ``json-unparseable (JSONDecodeError)`` with the
    envelope's own ``"}`` still glued to the payload's tail."""

    def test_recovered_from_every_output_shape(self):
        escaped = _pre_serialised(_monitor())
        shapes = {
            "Json.stdout": _update(rawOutput={"items": [{"Json": {"stdout": escaped}}]}),
            "Text": _update(rawOutput={"items": [{"Text": escaped}]}),
            "content block": _update(content=[{"content": {"type": "text", "text": escaped}}]),
        }
        for label, update in shapes.items():
            assert sd.peek(_runtime_output(update)) == (
                "monitor_start",
                MONITOR_ARGS,
            ), label

    def test_recovered_when_it_is_only_one_of_several_parts(self):
        # The live shape: prose beside the escaped envelope, so the JOINED text
        # does not parse as JSON and the recovery must work from the marker's own
        # line. This is what a whole-text json.loads alone cannot reach.
        out = _runtime_output(
            _update(
                content=[
                    {"content": {"type": "text", "text": "tool ran"}},
                    {"content": {"type": "text", "text": _pre_serialised(_monitor())}},
                ]
            )
        )
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS)
        assert out is not None and out.startswith("tool ran")

    def test_a_readable_directive_is_passed_through_byte_identical(self):
        marker = _monitor()
        out = _runtime_output(_update(content=[{"content": {"type": "text", "text": marker}}]))
        assert out == marker

    def test_plain_text_output_is_untouched(self):
        out = _runtime_output(_update(content=[{"content": {"type": "text", "text": "ok"}}]))
        assert out == "ok"


class TestSurvivesTheAcpClientParser:
    """``providers/acp.py``'s own builder, ``AcpClient._extract_tool_call_update``:
    a second, independent parser with the same envelope shapes and the same
    defect class."""

    @staticmethod
    def _output(update: dict[str, object]) -> str | None:
        fake = types.SimpleNamespace(_session_id="sess-1")
        msg = JsonRpcMessage(method="session/update", params={"update": update})
        event = AcpClient._extract_tool_call_update(fake, msg)
        return event.tool_output if event else None

    def test_directive_survives_unrecognised_envelope(self):
        out = self._output(_update(rawOutput={"items": [{"Json": {"result": _monitor()}}]}))
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS)

    def test_directive_survives_pre_serialised_text(self):
        out = self._output(_update(rawOutput={"items": [{"Text": _pre_serialised(_monitor())}]}))
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS)

    def test_a_readable_directive_is_passed_through_byte_identical(self):
        marker = _monitor()
        out = self._output(_update(content=[{"content": {"type": "text", "text": marker}}]))
        assert out == marker

    def test_marker_free_envelope_is_still_serialised(self):
        payload = {"exit_status": 0}
        out = self._output(_update(rawOutput={"items": [{"Json": payload}]}))
        assert out == json.dumps(payload, default=str)


class TestEveryToolResultBuilderRepairsTheMarker:
    """Ratchet: a NEW builder cannot skip the recovery.

    The defect was never one bad builder -- it was that several independent
    builders can emit an ``EVENT_TOOL_RESULT`` and the fix has to hold at each.
    The requirement is enforced here rather than left in a comment because the
    author who adds the next builder is a new provider's, and
    docs/system-specs/features/agent-host-contract.md §9 is the declaration they
    are meant to answer."""

    REQUIRED = "_repair_escaped_marker"

    @classmethod
    def _builders(cls) -> list[tuple[str, str, bool]]:
        """``(file, function, repairs?)`` per ``EVENT_TOOL_RESULT`` construction."""
        found: list[tuple[str, str, bool]] = []
        acp_dir = src_root() / "acp"
        for path, _text, tree in parsed_candidates(require_all=("EVENT_TOOL_RESULT",)):
            if acp_dir not in path.parents:
                continue
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                builds = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "AcpEvent"
                    and any(
                        kw.arg == "kind"
                        and isinstance(kw.value, ast.Name)
                        and kw.value.id == "EVENT_TOOL_RESULT"
                        for kw in node.keywords
                    )
                    for node in ast.walk(func)
                )
                if not builds:
                    continue
                repairs = any(
                    isinstance(node, ast.Name) and node.id == cls.REQUIRED
                    for node in ast.walk(func)
                )
                found.append((path.name, func.name, repairs))
        return found

    def test_every_builder_runs_the_repair(self):
        offenders = [f"{file}::{func}" for file, func, repairs in self._builders() if not repairs]
        assert not offenders, (
            f"these EVENT_TOOL_RESULT builders never call {self.REQUIRED} over "
            "their joined output, so a JSON-escaped session-directive marker "
            f"reaching them is dropped silently: {offenders}. See "
            "docs/system-specs/features/agent-host-contract.md §9."
        )

    def test_the_gate_sees_the_builders_it_is_meant_to_cover(self):
        # Without this, renaming AcpEvent (or breaking the AST match) would make
        # the gate above pass over an empty set.
        seen = {(file, func) for file, func, _ in self._builders()}
        assert ("_dispatch.py", "_build_tool_result_event") in seen
        assert ("client.py", "_extract_tool_call_update") in seen
        assert len(seen) >= 3, seen


# A value that appears NOWHERE else, so finding it in a log line proves the
# payload itself leaked rather than some incidental substring.
CANARY = "canary-9f3e2a-directive-body"


class TestRecoveryPreservesSurroundingOutput:
    """Finding 2: repairing the marker must not discard the rest of the frame.

    The marker has to leave on its own line for ``peek`` to read it, but the
    other fields are real tool output -- an exit status, a second text block --
    that the transcript is owed. A recovery that returns only the marker silently
    drops them, which is data loss the user cannot see or recover.
    """

    def test_json_branch_keeps_sibling_fields(self):
        # A marker-bearing envelope whose SIBLINGS carry real output.
        out = _runtime_output(
            _update(
                rawOutput={
                    # NOT `stdout`: that key takes the envelope's own
                    # long-standing shortcut, which drops siblings for every
                    # envelope and is not the marker path under test here.
                    "items": [{"Json": {"out": _monitor(), "exit_status": 7, "stderr": CANARY}}]
                }
            )
        )
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS), "selector still readable"
        assert "exit_status" in out and "7" in out, "sibling field survived the repair"
        assert CANARY in out, "sibling output survived the repair"

    def test_escaped_dump_keeps_sibling_fields(self):
        # The whole frame is one escaped dump: recovery path (1).
        repaired = _repair_escaped_marker(json.dumps({"out": _monitor(), "note": CANARY}))
        assert repaired is not None
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)
        assert CANARY in repaired, "the sibling field is not dropped for the marker"

    def test_partial_escape_keeps_head_and_tail(self):
        # An escaped dump sitting BESIDE other text: recovery path (2). Both the
        # prose before it and whatever trails the marker must survive.
        head = "step 1 done\n"
        tail = "\nstep 3 done: %s" % CANARY
        frame = head + json.dumps({"out": _monitor()})[1:-1] + tail
        repaired = _repair_escaped_marker(frame)
        assert repaired is not None
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)
        assert repaired.startswith(head), "leading output preserved"
        assert CANARY in repaired, "trailing output preserved"

    def test_marker_line_stays_a_single_json_value(self):
        # The suffix must land on a LATER line: peek parses the marker's own line
        # as one JSON value, so trailing bytes there would re-break the selector
        # this repair exists to restore.
        repaired = _repair_escaped_marker(
            json.dumps({"out": _monitor(), "note": CANARY})[1:-1] + "\ntrailing"
        )
        assert repaired is not None
        marker_line = [ln for ln in repaired.split("\n") if sd.SENTINEL in ln]
        assert len(marker_line) == 1
        payload = marker_line[0].split(sd.SENTINEL, 1)[1]
        json.loads(payload)  # raises if anything was glued onto the marker's line


class TestFailurePathDiagnosticsWithholdPayload:
    """Finding 1: the failure-path warnings run BEFORE redaction, so they must
    name the failure shape and never the payload bytes."""

    def test_peek_failure_reason_withholds_the_payload(self):
        reason = sd.peek_failure_reason(sd.SENTINEL + '{"kind": "monitor_start", ' + CANARY)
        assert "json-unparseable" in reason, reason
        assert "payload_len=" in reason and "payload_sha=" in reason, reason
        assert CANARY not in reason, "the malformed payload must not be echoed"

    def test_digest_is_stable_and_content_free(self):
        a = sd.content_free_digest(CANARY)
        assert a == sd.content_free_digest(CANARY), "same payload -> same handle"
        assert a != sd.content_free_digest(CANARY + "!"), "different payload -> different handle"
        assert CANARY not in a
        assert sd.content_free_digest("") == "empty", "printable without a special case"

    def test_repair_warning_withholds_the_frame(self, caplog):
        with caplog.at_level("WARNING"):
            out = _runtime_output(_update(rawOutput={"items": [{"Json": {"o": _monitor()}}]}))
        assert sd.peek(out) is not None, "the repair itself still works"
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert MONITOR_ARGS["message"] not in logged, "directive args reached the log"
        assert sd.SENTINEL not in logged, "the marker itself reached the log"

    def test_claim_miss_withholds_the_args(self, caplog):
        from kiro_crew.dashboard import directive_queue

        key = "sess-canary-1"
        directive_queue.reset()
        directive_queue.publish(key, "monitor_start", {"message": CANARY})
        with caplog.at_level("WARNING"):
            claimed = directive_queue.claim(key, "monitor_start", {"message": "something else"})
        assert claimed is None, "the mismatched record must not be claimed"
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "args-differ" in logged, "the operator still learns WHY it missed"
        assert CANARY not in logged, "the parked payload reached the log"


class TestOrdinaryResultsAreNotDuplicated:
    """A recognised MCP text envelope must pass through ONCE.

    The marker-envelope branch is selected by an explicit flag, not by comparing
    ``_mcp_content_text``'s return to itself: a >=2-block envelope returns a
    fresh ``"\\n".join`` each call, so an identity test reports "marker-bearing"
    for an ordinary result and emits the whole envelope a second time. A
    single-block envelope hides the bug (join returns the stored object), which
    is why the multi-block case is the one pinned here.
    """

    @staticmethod
    def _envelope(*texts: str) -> dict[str, object]:
        return {"content": [{"type": "text", "text": t} for t in texts]}

    def test_two_text_blocks_are_not_emitted_twice(self):
        out = _runtime_output(_update(rawOutput={"items": [{"Json": self._envelope("A", "B")}]}))
        assert out == "A\nB", out
        assert out.count("A") == 1 and out.count("B") == 1
        assert "content" not in out, "the envelope was dumped alongside its own text"

    def test_single_text_block_is_not_emitted_twice(self):
        out = _runtime_output(_update(rawOutput={"items": [{"Json": self._envelope("only")}]}))
        assert out == "only", out

    def test_multi_block_envelope_carrying_a_marker_still_resolves(self):
        out = _runtime_output(
            _update(rawOutput={"items": [{"Json": self._envelope("preamble", _monitor())}]})
        )
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS)
        assert "preamble" in out
        assert out.count("preamble") == 1, "duplicated on the marker path"


class TestPreservedOutputSurvivesDisplay:
    """Preserved output must survive ``strip_marker``, not just the repair.

    ``strip_marker`` truncates from the sentinel to the END of the string, so
    output placed AFTER the marker is recovered into ``tool_output`` and then
    dropped from the transcript the user actually reads -- preserved in the data
    and invisible in the product. Everything therefore goes BEFORE the marker,
    which keeps peek's line intact and keeps the bytes on the surviving side.
    """

    def test_json_branch_siblings_survive_strip(self):
        out = _runtime_output(
            _update(rawOutput={"items": [{"Json": {"out": _monitor(), "stderr": CANARY}}]})
        )
        assert sd.peek(out) is not None, "selector readable before display"
        shown = sd.strip_marker(out)
        assert CANARY in shown, "sibling output was cut by strip_marker"
        assert sd.SENTINEL not in shown, "the marker itself must not be displayed"

    def test_escaped_dump_siblings_survive_strip(self):
        repaired = _repair_escaped_marker(json.dumps({"out": _monitor(), "note": CANARY}))
        assert repaired is not None
        assert sd.peek(repaired) is not None
        assert CANARY in sd.strip_marker(repaired), "sibling output was cut by strip_marker"

    def test_partial_escape_head_and_suffix_survive_strip(self):
        head = "step 1 done\n"
        frame = head + json.dumps({"out": _monitor()})[1:-1] + "\nstep 3: %s" % CANARY
        repaired = _repair_escaped_marker(frame)
        assert repaired is not None
        assert sd.peek(repaired) is not None
        shown = sd.strip_marker(repaired)
        assert "step 1 done" in shown, "leading output was cut"
        assert CANARY in shown, "trailing output was cut by strip_marker"

    def test_marker_is_the_last_line(self):
        # The invariant that makes the two above hold, stated once directly.
        repaired = _repair_escaped_marker(json.dumps({"out": _monitor(), "note": CANARY}))
        assert repaired is not None
        lines = repaired.split("\n")
        assert sd.SENTINEL in lines[-1], "marker must be last, or strip_marker eats the rest"


class TestUnknownKindWithholdsThePayload:
    """`kind` is read out of model-visible marker text, so the diagnostic names
    its shape rather than echoing it -- the same rule as the excerpt beside it."""

    def test_unknown_kind_is_not_echoed(self):
        hostile = "not-a-tool-" + CANARY
        reason = sd.peek_failure_reason(
            sd.SENTINEL + json.dumps({"kind": hostile, "args": {}, "human": "x"})
        )
        assert reason.startswith("unknown-kind"), reason
        assert CANARY not in reason, "the payload's kind reached the log"
        assert "len=" in reason and "sha=" in reason, reason
