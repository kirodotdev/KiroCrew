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
import sys
import types

import pytest
from source_corpus import parsed_candidates, src_root

from kiro_crew import session_directive as sd
from kiro_crew.acp import _dispatch
from kiro_crew.acp._dispatch import (
    ELIDED_MARKER_VALUE,
    UNSERIALISABLE_SIBLING_VALUE,
    _build_tool_result_event,
    _elide_marker_value,
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


class TestSurvivesADuplicatingResultEnvelope:
    """The backend copies the whole result text into SEVERAL envelope fields.

    KAS returns ``{"response": <text>, "imageBase64Urls": [], "message": <same
    text>}``, so one directive arrives as two byte-identical marker-bearing
    strings and the frame's whole-text sentinel count is 2. Both the ambiguity
    refusal and ``_marker_bearing_text``'s single-match rule then declined, so no
    selector could be read, the gateway-parked record was never claimed, and a
    ``monitor_start`` the model had been told was requested armed no loop at all
    (reproduced from a live ``kirocrew-conductor`` frame). Two copies of ONE
    payload pose no choice, so there is nothing to guess between.
    """

    @staticmethod
    def _kas(text: str) -> str:
        """The live KAS envelope, pre-serialised as that backend hands it back."""
        dumped = json.dumps({"response": text, "imageBase64Urls": [], "message": text})
        assert dumped.count(sd.SENTINEL) == 2, "the duplication is the point"
        assert sd.peek(dumped) is None, "and the selector does not survive the dump"
        return dumped

    def test_duplicated_directive_is_recovered(self):
        repaired = _repair_escaped_marker(self._kas(_monitor()))
        assert repaired is not None, "a duplicated copy is not an ambiguous frame"
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)

    def test_duplicated_directive_is_recovered_through_the_runtime(self):
        # The path the conductor session actually took: the parser must hand the
        # consumer a frame whose selector reads, or nothing claims the record.
        for label, update in {
            "Json.stdout": _update(
                rawOutput={"items": [{"Json": {"stdout": self._kas(_monitor())}}]}
            ),
            "Text": _update(rawOutput={"items": [{"Text": self._kas(_monitor())}]}),
            "content block": _update(
                content=[{"content": {"type": "text", "text": self._kas(_monitor())}}]
            ),
        }.items():
            assert sd.peek(_runtime_output(update)) == ("monitor_start", MONITOR_ARGS), label

    def test_two_different_directives_across_fields_are_still_refused(self):
        # Deduping must key on the VALUE. Two fields carrying DIFFERENT payloads
        # are a real choice, and picking either applies a directive the tool did
        # not emit for this frame.
        other = sd.encode("monitor_start", {**MONITOR_ARGS, "idle_secs": 900}, "Armed: 900s.")
        frame = json.dumps({"response": _monitor(), "message": other})
        assert _repair_escaped_marker(frame) is None

    def test_one_field_holding_two_directives_is_refused(self):
        # The caller's multi-marker refusal guards only its line-based recovery,
        # so the envelope branch must reject a single string naming two
        # directives itself -- otherwise it resolves to whichever came first.
        other = sd.encode("monitor_start", {**MONITOR_ARGS, "idle_secs": 900}, "Armed: 900s.")
        frame = json.dumps({"response": _monitor() + "\n" + other})
        assert _repair_escaped_marker(frame) is None

    def test_a_bare_dumped_string_with_two_directives_is_refused(self):
        # Recovery (1) has TWO branches and both must hold the same bar: the
        # whole-text refusal that used to cover this one now guards only the
        # line-based recovery, and ``peek`` reads the FIRST marker line.
        other = sd.encode("monitor_start", {**MONITOR_ARGS, "idle_secs": 900}, "Armed: 900s.")
        assert _repair_escaped_marker(json.dumps(_monitor() + "\n" + other)) is None

    def test_a_bare_dumped_string_with_one_directive_is_recovered(self):
        # The refusal above must not cost the branch its normal case.
        repaired = _repair_escaped_marker(json.dumps(_monitor()))
        assert repaired is not None
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)

    def test_duplicated_frame_keeps_its_sibling_output(self):
        # Recovery must not trade the rest of the frame for the marker.
        frame = json.dumps({"response": _monitor(), "message": _monitor(), "note": CANARY})
        repaired = _repair_escaped_marker(frame)
        assert repaired is not None
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)
        assert CANARY in repaired


# One minimal payload per directive tool, in the shape that tool's own
# ``_emit_directive`` call in ``mcp_tools/control.py`` builds. Keyed by kind so
# the parametrised test below can assert coverage of every member of
# ``sd.DIRECTIVE_TOOLS`` rather than of a hand-copied list.
DIRECTIVE_PAYLOADS: dict[str, dict[str, object]] = {
    "monitor_start": MONITOR_ARGS,
    "monitor_watch": {
        "kind": "github_pull_request",
        "target": "https://github.com/o/r/pull/1",
        "objective": "review_ready",
        "cadence_secs": 300,
        "max_runtime_secs": 7200,
        "max_agent_turns": 4,
        "max_tokens": 200000,
        "max_provider_errors": 3,
        "wake_instructions": "report the first red check",
    },
    "monitor_update": {"patch": {"message": "poll the PR", "idle_secs": 600}},
    "monitor_stop": {"reason": "objective met"},
    "autonudge_stop": {"reason": "done"},
    "set_project": {"project": "/tmp/example-project", "clear": False},
    "suggest_followup": {
        "items": [{"title": "Add a test", "description": "cover the branch", "prompt": "do it"}]
    },
    "ask_question": {"questions": [{"question": "pick one", "options": [{"label": "a"}]}]},
    # The tool emits an empty payload: a caller asking for a clean context always
    # wants a clean one, so there is nothing to carry.
    "reset_conversation": {},
}


class TestRecoveryIsKindAgnostic:
    """The repair keys on the SENTINEL, never on the directive's kind, so every
    member of ``sd.DIRECTIVE_TOOLS`` must survive the duplicating envelope.

    Parametrised over the CONSTANT rather than a copied list, so a directive tool
    added later is covered the moment it joins the frozenset — and fails loudly
    here until someone gives it a payload. The five sibling tests above all carry
    a ``monitor_start`` payload, which proves the fix for one kind and says
    nothing about the other eight; this is the property that makes the fix
    general instead of incidental.
    """

    @pytest.mark.parametrize("kind", sorted(sd.DIRECTIVE_TOOLS))
    def test_every_directive_kind_survives_the_duplicating_envelope(self, kind):
        args = DIRECTIVE_PAYLOADS.get(kind)
        assert args is not None, (
            "%s joined sd.DIRECTIVE_TOOLS with no payload in DIRECTIVE_PAYLOADS — "
            "add its minimal args (the shape its own _emit_directive call builds) "
            "so this kind is actually covered" % kind
        )
        marker = sd.encode(kind, args, "%s requested." % kind)
        assert sd.peek(marker) == (kind, args), "fixture must start readable"
        # The live KAS envelope: the result text copied into two fields, then the
        # whole thing serialised.
        frame = json.dumps({"response": marker, "imageBase64Urls": [], "message": marker})
        assert frame.count(sd.SENTINEL) == 2
        assert sd.peek(frame) is None, "the dump destroys the selector"
        repaired = _repair_escaped_marker(frame)
        assert repaired is not None, "%s was not recovered" % kind
        assert sd.peek(repaired) == (kind, args)

    @pytest.mark.parametrize("kind", sorted(sd.DIRECTIVE_TOOLS))
    def test_every_directive_kind_survives_the_runtime_parser(self, kind):
        # Through the real parser, which is the path a session actually takes.
        marker = sd.encode(kind, DIRECTIVE_PAYLOADS[kind], "%s requested." % kind)
        frame = json.dumps({"response": marker, "imageBase64Urls": [], "message": marker})
        out = _runtime_output(_update(rawOutput={"items": [{"Json": {"stdout": frame}}]}))
        assert sd.peek(out) == (kind, DIRECTIVE_PAYLOADS[kind])


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
    docs/system-specs/modules/agent-host-contract.md §9 is the declaration they
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
            "docs/system-specs/modules/agent-host-contract.md §9."
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


def _nest(depth: int, leaf: object) -> dict[str, object]:
    """A ``depth``-deep chain of single-key dicts ending in *leaf*."""
    node: object = {"leaf": leaf}
    for _ in range(depth):
        node = {"n": node}
    assert isinstance(node, dict)
    return node


def _leaf(node: object) -> object:
    """Walk a :func:`_nest` chain iteratively -- a recursive reader has the bug."""
    while isinstance(node, dict) and "n" in node:
        node = node["n"]
    return node["leaf"] if isinstance(node, dict) else node


def _depth_of(node: object) -> int:
    """Nesting depth of *node*, measured without recursing."""
    deepest = 0
    stack: list[tuple[object, int]] = [(node, 1)]
    while stack:
        current, level = stack.pop()
        deepest = max(deepest, level)
        if isinstance(current, dict):
            stack.extend((v, level + 1) for v in current.values())
        elif isinstance(current, list):
            stack.extend((v, level + 1) for v in current)
    return deepest


def _encoder_refusing_past(limit: int):
    """A ``json`` stand-in whose ``dumps`` raises like the C encoder does.

    The real encoder recurses in C against the PROCESS stack, so the depth it
    refuses at is a property of the platform's thread stack size rather than of
    ``sys.recursionlimit``: a branch that encodes on Linux raises on Windows. A
    test that reproduces the overflow by NESTING therefore passes for the wrong
    reason on the machine most people run it on and reds only on the Windows
    shard. Raising the encoder's error directly makes the degrade path
    deterministic everywhere.

    Returned as a whole module stand-in, patched over ``_dispatch.json``, so the
    injected fault reaches only the module under test -- ``session_directive``
    parses the marker with the real ``json`` on the same call.
    """
    real = json.dumps

    def _dumps(obj, **kwargs):
        if _depth_of(obj) > limit:
            raise RecursionError("maximum recursion depth exceeded")
        return real(obj, **kwargs)

    return types.SimpleNamespace(dumps=_dumps, loads=json.loads, JSONDecoder=json.JSONDecoder)


class TestPathologicallyNestedEnvelopeDegrades:
    """A frame too deep for the walk or the encoder must degrade, not kill the turn.

    The nesting depth comes out of a TOOL's own output, so whatever produced the
    frame chooses it. Two independent limits sit on the marker path, and both
    raise ``RecursionError`` -- a ``RuntimeError``, so the module's
    ``(ValueError, TypeError)`` handlers do not catch it and it escapes
    ``parse_session_update`` and aborts the whole agent turn:

    * the elision walk that copies the envelope's siblings, bounded by
      ``sys.recursionlimit`` while it recurses;
    * ``json.dumps`` of that copy, bounded by the process stack instead.

    The directive is what must survive either one. Losing it silently unarms a
    loop the model was told was armed; losing sibling detail costs transcript
    content the user can see is missing.
    """

    def test_elision_walk_survives_depth_past_the_recursion_limit(self):
        # The walk is pure Python, so sys.recursionlimit bounds it -- which makes
        # this depth deterministic on every platform, unlike the encoder's.
        marker = _monitor()
        payload = {"out": marker, "sib": _nest(sys.getrecursionlimit() * 3, CANARY)}
        copied = _elide_marker_value(payload, marker)
        assert copied["out"] == ELIDED_MARKER_VALUE, "the marker value is elided"
        assert _leaf(copied["sib"]) == CANARY, "the deep sibling branch is copied whole"
        assert copied is not payload and copied["sib"] is not payload["sib"], "it is a copy"

    def test_elision_walk_preserves_key_order(self):
        # The iterative walk reserves each slot before pushing the child, so the
        # copy dumps in the source's field order rather than the pop order.
        marker = _monitor()
        payload = {"z": 1, "out": marker, "a": [2, {"b": 3}]}
        assert list(_elide_marker_value(payload, marker)) == ["z", "out", "a"]

    def test_deep_envelope_yields_a_readable_directive(self):
        # End to end, at a depth the recursive walk could not take. Only the
        # directive is asserted: whether the REAL encoder also refuses this depth
        # is platform-dependent, and the frame must stay readable either way.
        out = _runtime_output(
            _update(
                rawOutput={
                    "items": [
                        {"Json": {"out": _monitor(), "sib": _nest(sys.getrecursionlimit() * 3, 1)}}
                    ]
                }
            )
        )
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS), "the directive stays readable"

    def test_encoder_refusal_keeps_the_directive(self, monkeypatch):
        # The encoder error raised DIRECTLY -- see _encoder_refusing_past.
        monkeypatch.setattr(_dispatch, "json", _encoder_refusing_past(0))
        out = _runtime_output(
            _update(rawOutput={"items": [{"Json": {"out": _monitor(), "sib": {"deep": 1}}}]})
        )
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS), "the directive still arms the loop"

    def test_encoder_refusal_keeps_the_siblings_it_can_encode(self, monkeypatch):
        # An all-or-nothing bail drops SHALLOW sibling output the encoder had no
        # trouble with, so the degrade is per field.
        monkeypatch.setattr(_dispatch, "json", _encoder_refusing_past(4))
        out = _runtime_output(
            _update(
                rawOutput={
                    "items": [
                        {
                            "Json": {
                                "out": _monitor(),
                                "stderr": CANARY,
                                "exit_status": 7,
                                "sib": _nest(40, 1),
                            }
                        }
                    ]
                }
            )
        )
        assert sd.peek(out) == ("monitor_start", MONITOR_ARGS), "the directive survives"
        assert CANARY in out and "exit_status" in out, "encodable siblings survive"
        assert UNSERIALISABLE_SIBLING_VALUE in out, "the refused field is named, not dropped"

    def test_repair_survives_a_scanner_refusal(self, monkeypatch):
        # json.loads' C scanner raises RecursionError on text nested past its own
        # limit, and RecursionError is a RuntimeError -- outside the ValueError
        # family that handler names. Raised directly for the same reason the
        # encoder's is: the depth it gives up at is a platform property.
        frame = json.dumps({"out": _monitor(), "note": CANARY})

        def _refuse(*_args, **_kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(
            _dispatch,
            "json",
            types.SimpleNamespace(dumps=json.dumps, loads=_refuse, JSONDecoder=json.JSONDecoder),
        )
        repaired = _repair_escaped_marker(frame)
        assert repaired is not None, "the escaped-line recovery still runs"
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)

    def test_repair_encoder_refusal_returns_the_directive_alone(self, monkeypatch):
        frame = json.dumps({"out": _monitor(), "note": CANARY})
        monkeypatch.setattr(_dispatch, "json", _encoder_refusing_past(0))
        repaired = _repair_escaped_marker(frame)
        assert repaired is not None, "the directive is recovered even with no siblings"
        assert sd.peek(repaired) == ("monitor_start", MONITOR_ARGS)

    def test_parse_session_update_does_not_abort_the_turn(self, monkeypatch):
        # The seam the RecursionError escaped through. Both limits at once.
        monkeypatch.setattr(_dispatch, "json", _encoder_refusing_past(2))
        events = parse_session_update(
            _update(
                rawOutput={
                    "items": [
                        {"Json": {"out": _monitor(), "sib": _nest(sys.getrecursionlimit() * 3, 1)}}
                    ]
                }
            ),
            cache_scope="scope",
        )
        assert [e for e in events if e.kind == EVENT_TOOL_RESULT], "the result event still arrives"
