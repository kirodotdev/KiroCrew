"""#8962: ``preserve_tail_marker`` must re-attach the MARKER, not the last
occurrence of the sentinel *substring*.

The marker's payload is model-authored (``monitor_start``'s ``message``,
``autonudge_stop``'s ``reason``) and JSON string escaping leaves ``[`` alone, so
a directive whose own arguments carry the literal sentinel bytes places a later
occurrence of the sentinel INSIDE the payload. ``rfind`` selected that embedded
occurrence, the preserved "tail" began mid-payload, and the re-attached frame
was unreadable: the helper that exists to protect the marker was the thing that
corrupted it, and the effect (a monitor loop, a project switch) was silently
dropped while the model was told it had been made.

The fix walks sentinel occurrences from the right and accepts the first whose
tail actually READS (``peek`` for the directive sentinel; exact tail anchoring
for the refusal tag, which :func:`tag_refusal` appends as the final line). Two
occurrences reading as genuinely DIFFERENT markers are refused outright -- the
same ambiguity bar ``_repair_escaped_marker`` holds, so this seam cannot be
used to launder a two-marker frame into a clean one.
"""

from __future__ import annotations

from kiro_crew import session_directive

MAX = session_directive.MAX_TOOL_RESULT_CHARS
SENTINEL = session_directive.SENTINEL


def _cut_dropping_the_tail(full: str) -> str:
    """The transport's naive length cut, sized so the marker line is lost."""
    cut = full[:MAX]
    assert not full == cut, "precondition: a real truncation occurred"
    return cut


class TestTheMarkerWinsOverEmbeddedSentinelBytes:
    def test_a_directive_whose_message_carries_the_sentinel_round_trips(self):
        """THE #8962 attack: sentinel bytes inside ``args`` must not divert the
        re-attach to a mid-payload tail."""
        args = {"message": f"alert when {SENTINEL} shows up in the output"}
        directive = session_directive.encode("monitor_start", args, "Monitoring armed")
        assert directive.count(SENTINEL) == 2, "precondition: payload embeds the sentinel"
        full = "y" * MAX + "\n" + directive
        cut = _cut_dropping_the_tail(full)
        assert session_directive.peek(cut) is None, "precondition: the cut drops the marker"

        kept = session_directive.preserve_tail_marker(full, cut)

        got = session_directive.peek(kept)
        assert got is not None, "the re-attached frame must be readable"
        kind, got_args = got
        assert kind == "monitor_start"
        assert got_args == args, "the payload must survive byte-for-byte"
        assert len(kept) <= MAX

    def test_a_well_formed_frame_is_preserved_exactly_as_before(self):
        """Control: no embedded sentinel, the walk lands on the same occurrence
        ``rfind`` always chose."""
        directive = session_directive.encode("autonudge_stop", {"reason": "goal met"}, "stopping")
        full = "y" * MAX + "\n" + directive
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "goal met"}
        assert len(kept) <= MAX

    def test_two_copies_of_the_same_marker_are_not_ambiguous(self):
        """A backend that duplicates the frame raises the occurrence count
        without naming two directives -- ``_repair_escaped_marker`` resolves that
        case rather than refusing, and this seam must agree."""
        directive = session_directive.encode("autonudge_stop", {"reason": "done"}, "stopping")
        marker_line = directive.split("\n", 1)[1]
        full = "y" * MAX + "\n" + marker_line + "\n" + marker_line
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.peek(kept) == ("autonudge_stop", {"reason": "done"})


class TestAmbiguityIsRefusedNotLaundered:
    def test_two_different_readable_markers_are_left_cut(self):
        """The pin the issue asks for: a frame naming two genuinely different
        directives must not have one of them picked and re-attached --
        ``_repair_escaped_marker`` refuses that frame when it arrives whole, and
        a length cut must not become the way around that refusal."""
        a = session_directive.encode("autonudge_stop", {"reason": "a"}, "ha")
        b = session_directive.encode("monitor_start", {"message": "b"}, "hb")
        full = "y" * MAX + "\n" + a + "\n" + b
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut, "ambiguous frames are refused, not resolved"


class TestNothingReadableMeansNothingReattached:
    def test_prose_mentioning_the_sentinel_is_not_promoted_to_a_marker(self):
        """Sentinel bytes with no readable marker anywhere (a file read, a doc
        quoting the constant) used to get a garbage tail re-attached at the cost
        of the prose the cut had kept. Nothing a consumer could read is being
        protected, so the cut stands."""
        full = "y" * MAX + "\n note: " + SENTINEL + "zzz is the marker prefix"
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut

    def test_a_refusal_tag_survives_embedded_directive_bytes_in_its_prose(self):
        """A tagged refusal whose prose carries directive-sentinel bytes must
        fall through the (unreadable) directive occurrences and still get its
        tail-anchored tag back -- losing it re-creates the lost-marker class
        the tag exists to prevent."""
        refusal = session_directive.tag_refusal(f"Error: field {SENTINEL}zzz was rejected")
        full = "y" * MAX + "\n" + refusal
        cut = _cut_dropping_the_tail(full)
        assert not session_directive.is_refusal(cut), "precondition: the cut drops the tag"

        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.is_refusal(kept)
        assert session_directive.peek(kept) is None, "the prose bytes must not read as a directive"
        assert len(kept) <= MAX
