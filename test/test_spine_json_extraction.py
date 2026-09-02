"""The spine's agent-reply JSON extraction, consolidated onto the shared
``llm_helpers._extract_json_of_type`` scanner (#4974).

Locks the call-site behaviors ``_extract_json_array`` / ``_has_json_array``
provide to ``_discover_surfaces_via_agent``: fenced replies, stray bracketed
prose (the input class outermost-span scanners mishandle), object-record
preference, the shared ambiguity refusal, and the top-level-only contract the
tool-side forcing re-emit relies on. Pure-function tests — no agent, no I/O.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.auto_improvement.spine.agent_discovery import (
    _extract_json_array,
    _has_json_array,
    discover_surfaces_via_agent,
)


class TestExtractJsonArray:
    def test_bare_array_of_objects(self) -> None:
        assert _extract_json_array('[{"file": "a.py"}]') == [{"file": "a.py"}]

    def test_fenced_array(self) -> None:
        text = 'Here you go:\n```json\n[{"file": "a.py", "line": 3}]\n```\nDone.'
        assert _extract_json_array(text) == [{"file": "a.py", "line": 3}]

    def test_stray_bracketed_prose_before_array(self) -> None:
        # "see line [12]" parses as a JSON array of ints and appears FIRST; the
        # object-record preference selects the real findings array after it.
        text = 'see line [12] for context. Findings: [{"file": "a.py"}]'
        assert _extract_json_array(text) == [{"file": "a.py"}]

    def test_stray_bracketed_prose_after_array(self) -> None:
        text = '[{"file": "a.py"}] as noted in [3] above'
        assert _extract_json_array(text) == [{"file": "a.py"}]

    def test_non_dict_items_filtered(self) -> None:
        assert _extract_json_array('["x", {"file": "a.py"}, 3]') == [{"file": "a.py"}]

    def test_scalar_only_array_yields_empty(self) -> None:
        assert _extract_json_array("[1, 2, 3]") == []

    def test_empty_array_yields_empty(self) -> None:
        assert _extract_json_array("[]") == []

    def test_no_array_yields_empty(self) -> None:
        assert _extract_json_array("no json here") == []
        assert _extract_json_array("") == []

    def test_malformed_array_yields_empty(self) -> None:
        assert _extract_json_array('[{"file": "a.py"') == []

    def test_restated_identical_array_is_not_ambiguous(self) -> None:
        # Shared contract: equal preferred matches collapse to one.
        text = '[{"file": "a.py"}]\nAs stated: [{"file": "a.py"}]'
        assert _extract_json_array(text) == [{"file": "a.py"}]

    def test_two_different_object_arrays_refuse_to_guess(self) -> None:
        # Shared contract: two DIFFERENT preferred matches return None → [].
        # A first-match scan would execute the worked example that precedes
        # the real payload; refusing is the safer failure.
        text = 'e.g. [{"file": "ex.py"}] ... final: [{"file": "real.py"}]'
        assert _extract_json_array(text) == []

    def test_array_inside_object_wrapper_not_dug_out(self) -> None:
        # Top-level-only contract: an array nested in an object wrapper is not
        # extracted, and _has_json_array is False for it — so the tool-side
        # forcing re-emit fires and demands the bare array (the designed
        # recovery for format deviations).
        text = '{"findings": [{"file": "a.py"}]}'
        assert _extract_json_array(text) == []
        assert _has_json_array(text) is False


class TestHasJsonArray:
    def test_empty_array_counts_as_answered(self) -> None:
        # `[]` is a legitimate "no findings" answer and must NOT trigger the
        # forcing re-emit ([] is falsy — the check must be `is not None`).
        assert _has_json_array("[]") is True

    def test_fenced_empty_array_counts(self) -> None:
        assert _has_json_array("```json\n[]\n```") is True

    def test_array_after_prose_counts(self) -> None:
        assert _has_json_array('analysis done. [{"file": "a.py"}]') is True

    def test_prose_only_is_false(self) -> None:
        assert _has_json_array("I could not finish reading the files") is False
        assert _has_json_array("") is False

    def test_object_only_reply_is_false(self) -> None:
        assert _has_json_array('{"a": 1}') is False

    def test_scalar_prose_array_is_not_an_answer(self) -> None:
        # "see line [12]" parses as a JSON array but is prose, not an answer —
        # counting it suppressed the forcing re-emit (pre-push review finding).
        assert _has_json_array("see line [12] for details") is False

    def test_wrapper_plus_prose_array_still_reads_unanswered(self) -> None:
        # The convergent pre-push review finding (GPT + Opus): an object-wrapped
        # reply next to stray bracketed prose must NOT read as answered, or the
        # wrapped findings are silently dropped instead of recovered by the
        # forcing re-emit.
        text = '{"findings": [{"file": "a.py"}]} See line [12].'
        assert _extract_json_array(text) == []
        assert _has_json_array(text) is False

    def test_wrapper_plus_instruction_echo_empty_array_reads_unanswered(self) -> None:
        # GPT round 3: an empty [] embedded in prose is instruction-echo, not a
        # no-findings answer — counting it suppressed the forcing re-emit and
        # lost the wrapped findings. [] answers only as the whole reply.
        text = '{"findings": [{"file": "a.py"}]} Use [] when none.'
        assert _extract_json_array(text) == []
        assert _has_json_array(text) is False

    def test_whole_reply_scalar_array_counts_as_answered(self) -> None:
        # The agent complied with the demanded form (the reply IS the array);
        # re-asking cannot improve on it, so no forcing — extraction just
        # filters the non-dict items away.
        assert _extract_json_array("[1, 2, 3]") == []
        assert _has_json_array("[1, 2, 3]") is True


class TestAdversarialNesting:
    # Untrusted model output can embed pathological nesting; the shared
    # scanner must keep both call-site contracts (ValueError / never-raises)
    # instead of letting RecursionError escape (pre-push review finding).

    def test_unterminated_nesting_bomb_never_raises(self) -> None:
        assert _extract_json_array("x " + "[" * 100_000) == []

    def test_balanced_nesting_bomb_never_raises(self) -> None:
        assert _extract_json_array("x " + "[" * 100_000 + "]" * 100_000) == []

    def test_payload_before_bomb_fails_closed(self) -> None:
        # A reply containing a nesting bomb yields nothing, even when a payload
        # precedes the bomb: a truncated scan cannot certify the payload as
        # unambiguous (a later DIFFERENT payload may follow the bomb), and the
        # forcing re-emit is the designed recovery. GPT review round 4 — this
        # flips the earlier keep-the-prefix behavior, which let a worked
        # example launder past the ambiguity refusal.
        text = '[{"file": "a.py"}] ' + "[" * 100_000
        assert _extract_json_array(text) == []
        assert _has_json_array(text) is False


class TestForcingReemitWiring:
    def test_wrapper_plus_prose_reply_triggers_forced_reemit(self, tmp_path) -> None:
        # End-to-end through discover_surfaces_via_agent with a stub runner:
        # call 1 returns the wrapper+prose reply, the forcing re-emit (call 2,
        # no tools) returns the bare array, and the finding is recovered.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")

        class _Res:
            def __init__(self, text: str) -> None:
                self.text = text
                self.ok = True
                self.error = ""

        calls: list[dict] = []

        class _Runner:
            def run(self, prompt: str, **kwargs) -> _Res:
                calls.append(kwargs)
                if len(calls) == 1:
                    return _Res('{"findings": [{"file": "src/x.py"}]} See line [12].')
                return _Res('[{"file": "src/x.py", "line": 1, "symbol": "x", "message": "m"}]')

        out = discover_surfaces_via_agent(_Runner(), clone=tmp_path)
        assert len(calls) == 2, "forcing re-emit must fire for a wrapper+prose reply"
        assert calls[1].get("allowed_tools") == []  # the re-emit call carries no tools
        assert [s["file"] for s in out] == ["src/x.py"]

    def test_empty_array_answer_does_not_reemit(self, tmp_path) -> None:
        # A bare [] is a completed no-findings answer: exactly one call.
        calls: list[dict] = []

        class _Res:
            text = "[]"
            ok = True
            error = ""

        class _Runner:
            def run(self, prompt: str, **kwargs) -> _Res:
                calls.append(kwargs)
                return _Res()

        out = discover_surfaces_via_agent(_Runner(), clone=tmp_path)
        assert len(calls) == 1
        assert out == []
