"""``preserve_tail_marker`` must re-attach the MARKER, not the last
occurrence of the sentinel *substring*.

The marker's payload is model-authored (``monitor_start``'s ``message``,
``autonudge_stop``'s ``reason``) and JSON string escaping leaves ``[`` alone, so
a directive whose own arguments carry the literal sentinel bytes places a later
occurrence of the sentinel INSIDE the payload. A plain rightmost-substring
search lands on that embedded occurrence, so the "tail" begins mid-payload and
the re-attached frame reads to no consumer -- the effect (a monitor loop, a
project switch) is silently dropped while the model is told it was made.

The walk scans sentinel occurrences from the right and accepts the first whose
tail actually READS as a genuine directive marker (its JSON names a known
directive tool; exact tail anchoring for the refusal tag, which
:func:`tag_refusal` appends as the final line). Two
occurrences reading as genuinely DIFFERENT markers are refused outright, so a
length cut cannot launder a two-marker frame into a clean one. The refusal
is fail-safe: ``encode`` cannot produce two different readable markers.
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
        """THE embedded-sentinel attack: sentinel bytes inside ``args`` must not
        divert the re-attach to a mid-payload tail."""
        args = {"message": f"alert when {SENTINEL} shows up in the output"}
        directive = session_directive.encode("monitor_start", args, "Monitoring armed")
        assert directive.count(SENTINEL) == 2, "precondition: payload embeds the sentinel"
        full = "y" * MAX + "\n" + directive
        cut = _cut_dropping_the_tail(full)
        assert (
            session_directive.decode(cut, "monitor_start") is None
        ), "precondition: the cut drops the marker"

        kept = session_directive.preserve_tail_marker(full, cut)

        got_args = session_directive.decode(kept, "monitor_start")
        assert got_args is not None, "the re-attached frame must be readable"
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
        without naming two directives -- identical duplicate lines resolve to the
        rightmost rather than refusing, and this seam must agree."""
        directive = session_directive.encode("autonudge_stop", {"reason": "done"}, "stopping")
        marker_line = directive.split("\n", 1)[1]
        full = "y" * MAX + "\n" + marker_line + "\n" + marker_line
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "done"}


class TestAmbiguityIsRefusedNotLaundered:
    def test_two_different_readable_markers_are_left_cut(self):
        """The pin the issue asks for: a frame naming two genuinely different
        directives must not have one of them picked and re-attached -- this seam
        refuses the whole frame, and a length cut must not become a way around
        that refusal."""
        a = session_directive.encode("autonudge_stop", {"reason": "a"}, "ha")
        b = session_directive.encode("monitor_start", {"message": "b"}, "hb")
        full = "y" * MAX + "\n" + a + "\n" + b
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut, "ambiguous frames are refused, not resolved"


class TestNothingReadableMeansNothingReattached:
    def test_prose_mentioning_the_sentinel_is_not_promoted_to_a_marker(self):
        """Sentinel bytes with no readable marker anywhere (a file read, a doc
        quoting the constant) must not get a garbage tail re-attached at the cost
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
        assert (
            session_directive.decode(kept, "monitor_start") is None
        ), "the prose bytes must not read as a directive"
        assert len(kept) <= MAX


class TestRepeatedSentinelBytesStayLinear:
    """The locate walk must pay a BOUNDED cost per occurrence, never a suffix of
    the frame. ``full`` is an unbounded, model-authored tool-result join (the
    per-part cut is deliberately gone at the dispatch seam), and the sentinel is
    a public constant -- one command whose output repeats it puts tens of
    thousands of occurrences in a multi-megabyte frame. A per-occurrence
    ``full[probe:]`` slice makes that O(N*L): an event-loop stall and a watchdog
    restart, reachable without a single valid marker.
    """

    def test_a_frame_dense_with_sentinel_bytes_parses_in_bounded_time(self):
        """Attack shape from review: repeated sentinel bytes, no readable marker
        until the genuine one at the tail. Quadratic locate stalls for minutes;
        the bounded walk finishes with a wide margin under the ceiling."""
        import time

        directive = session_directive.encode("autonudge_stop", {"reason": "goal met"}, "stopping")
        # ~1.3 MB of prose carrying ~32,000 sentinel occurrences on one line --
        # the review's own attack shape. Each occurrence's "line" is the giant
        # remainder, so a suffix-slicing walk pays ~40 GB of copies (measured
        # ~27s here; the ceiling below under-states it 5x on purpose). The
        # noise sits BEYOND the cut, keeping the kept head sentinel-free prose:
        # the walk runs over ``full`` either way, and a sentinel-free head is
        # what lets the round-trip assert stay byte-exact. The fixed walk stays
        # ~milliseconds (lesson: CI fixture cost must not become the test's own
        # failure mode).
        noise = (SENTINEL + "x" * 32) * 32000
        full = "y" * MAX + "\n" + noise + "\n" + directive
        cut = _cut_dropping_the_tail(full)

        start = time.monotonic()
        kept = session_directive.preserve_tail_marker(full, cut)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"locate walk took {elapsed:.1f}s -- quadratic cost is back"
        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "goal met"}

    def test_sentinel_dense_frame_with_no_marker_at_all_is_also_bounded(self):
        """Same attack without any genuine marker: the walk still visits every
        occurrence (the ambiguity bar requires it), so the bound must hold on
        the pure-noise path too, and the cut must stand untouched."""
        import time

        noise = (SENTINEL + "x" * 32) * 32000
        full = "y" * MAX + "\n" + noise + "\nplain tail, no marker"
        cut = _cut_dropping_the_tail(full)

        start = time.monotonic()
        kept = session_directive.preserve_tail_marker(full, cut)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"locate walk took {elapsed:.1f}s -- quadratic cost is back"
        assert kept == cut


class TestAMarkerLineWiderThanAFrameIsBytes:
    def test_an_over_budget_marker_line_cannot_divert_the_reattach(self):
        """A sentinel line longer than MAX_TOOL_RESULT_CHARS cannot be a marker
        three ways at once: ``encode`` refuses anything over MAX_DIRECTIVE_CHARS,
        the room check could never re-attach it, and the transport cut means no
        consumer ever reads it whole. The bounded walk treats it as the bytes it
        is, and the genuine, encodable marker still wins."""
        directive = session_directive.encode("autonudge_stop", {"reason": "goal met"}, "stopping")
        huge_args = '{"kind":"monitor_start","args":{"message":"' + "m" * (MAX * 2) + '"}}'
        full = "y" * MAX + "\n" + SENTINEL + huge_args + "\n" + directive
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "goal met"}
        assert len(kept) <= MAX

    def test_an_over_budget_line_alone_leaves_the_cut_standing(self):
        """The over-budget line as the ONLY sentinel content: nothing readable
        within the frame budget, so nothing is re-attached."""
        huge_args = '{"kind":"monitor_start","args":{"message":"' + "m" * (MAX * 2) + '"}}'
        full = "y" * MAX + "\n" + SENTINEL + huge_args
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut


class TestAMalformedKindIsNotACrash:
    def test_an_unhashable_kind_is_treated_as_bytes_not_a_marker(self):
        """A ``kind`` that is not a string (``{"kind": []}``) is model-authored
        tool-output bytes any command can print. The occurrence-reads check must
        treat it as bytes, never let it reach the ``frozenset`` membership test
        where an unhashable value would raise ``TypeError`` and crash tool-result
        processing."""
        directive = session_directive.encode("autonudge_stop", {"reason": "goal met"}, "stopping")
        bad = SENTINEL + '{"kind":[]}'
        full = "y" * MAX + "\n" + bad + "\n" + directive
        cut = _cut_dropping_the_tail(full)

        # Must not raise, and the genuine marker still wins.
        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "goal met"}
        assert len(kept) <= MAX

    def test_an_unhashable_kind_alone_leaves_the_cut_standing(self):
        """The malformed line as the ONLY sentinel content: nothing readable, so
        nothing is re-attached and nothing crashes."""
        full = "y" * MAX + "\n" + SENTINEL + '{"kind":{"nested":1}}'
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut


class TestManyDistinctMarkersDoNotAccumulate:
    def test_many_different_readable_markers_refuse_without_unbounded_state(self):
        """A sentinel-dense frame with MANY distinct valid marker lines must be
        refused (two different markers is ambiguous) without retaining a set that
        grows with the occurrence count -- the walk returns on the first
        divergence, so the kept state is one bounded line."""
        # 500 distinct genuine markers, each a valid directive line.
        markers = "\n".join(
            session_directive.encode("autonudge_stop", {"reason": f"r{i}"}, "s").split("\n", 1)[1]
            for i in range(500)
        )
        full = "y" * MAX + "\n" + markers
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        # Two genuinely different markers -> refuse (the cut stands).
        assert kept == cut


class TestDeeplyNestedBracketsDoNotCrash:
    def test_a_bracket_bomb_after_the_sentinel_is_treated_as_bytes(self):
        """Model-authored output can nest brackets thousands deep; json.loads
        recurses per level and raises RecursionError (a RuntimeError, not
        ValueError/TypeError). The occurrence-reads check must catch it and
        treat the frame as bytes, never let it propagate and kill the turn."""
        directive = session_directive.encode("autonudge_stop", {"reason": "goal met"}, "stopping")
        bomb = SENTINEL + "[" * 4000
        full = "y" * MAX + "\n" + bomb + "\n" + directive
        cut = _cut_dropping_the_tail(full)

        # Must not raise (RecursionError would otherwise abort the turn).
        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "goal met"}
        assert len(kept) <= MAX

    def test_a_bracket_bomb_alone_leaves_the_cut_standing(self):
        """The bracket bomb as the ONLY sentinel content: nothing readable, so
        nothing is re-attached and nothing crashes."""
        full = "y" * MAX + "\n" + SENTINEL + "[" * 4000
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut


class TestAnUnknownStringKindIsNotAMarker:
    def test_a_valid_json_object_with_an_unknown_kind_is_not_promoted(self):
        """A syntactically valid marker line whose kind is a STRING but not a
        known directive tool must not read as a marker: the membership test is
        the bar, and an unknown kind matches no record. Pins that the check is
        `kind in DIRECTIVE_TOOLS`, not merely `kind is present`."""
        directive = session_directive.encode("autonudge_stop", {"reason": "goal met"}, "stopping")
        bogus = SENTINEL + '{"kind":"not_a_directive_tool","args":{}}'
        full = "y" * MAX + "\n" + bogus + "\n" + directive
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        # The unknown-kind occurrence is skipped; the genuine directive wins.
        assert session_directive.decode(kept, "autonudge_stop") == {"reason": "goal met"}
        assert len(kept) <= MAX

    def test_an_unknown_string_kind_alone_leaves_the_cut_standing(self):
        """The unknown-kind line as the ONLY sentinel content reads as no marker,
        so nothing is re-attached."""
        full = "y" * MAX + "\n" + SENTINEL + '{"kind":"totally_unknown","args":{}}'
        cut = _cut_dropping_the_tail(full)

        kept = session_directive.preserve_tail_marker(full, cut)

        assert kept == cut


class TestAnEnvelopedRefusalTagIsReattached:
    def test_a_refusal_tag_not_strictly_at_the_tail_is_still_reattached(self):
        """A backend can serialise the result inside an envelope, so the refusal
        tag sits mid-string rather than strictly at the frame's end. The refusal
        branch takes the rightmost occurrence (the tag carries no payload and
        grants nothing), so is_refusal stays true and the by-design decline does
        not read as a lost-marker regression."""
        refusal = session_directive.tag_refusal("Error: field rejected")
        # An envelope shape: the tagged text wrapped, with trailing envelope bytes
        # after the refusal sentinel so it is not strictly the last characters.
        full = "y" * MAX + '\n{"response":"' + refusal + '","message":"ok"}'
        cut = _cut_dropping_the_tail(full)
        assert not session_directive.is_refusal(cut), "precondition: the cut drops the tag"

        kept = session_directive.preserve_tail_marker(full, cut)

        assert session_directive.is_refusal(kept), "the enveloped refusal tag must be re-attached"
        assert len(kept) <= MAX
