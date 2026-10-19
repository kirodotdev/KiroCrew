"""The streaming probe and the complete action grammar agree, over a GENERATED corpus.

``split_trailing_protocol_suffix`` asks two different patterns about a reply's tail:
``OPTION_ACTIONS_RE_TRAILER`` decides "is this a finished marker?", and
``_OPTION_ACTIONS_TAIL_PREFIX_RE`` decides "could this still become one?". A
detached suffix is discarded rather than rendered, so the two must agree in BOTH
directions, and hand-picked examples cannot show agreement on shapes nobody listed:

* probe too STRICT -- a genuinely streaming marker is published as raw text.
* probe too LOOSE -- a tail that can never finish is detached, and the reply's
  end is lost with no error.

Both patterns are now built from ``_MARKER_ACTION_BODY_ATOM``, so the agreement is
structural; these tests are what detects a future edit breaking that.

The two directions together are a biconditional: a tail is held if and ONLY if some
continuation completes it. Direction two is stated over the completion SEARCH rather
than over prefixes of markers inside the corpus, because a completion can be longer
than the corpus generates: an open pair holding a separator needs six more bytes, so
a corpus of four-byte bodies cannot contain the finished marker and a prefix-only
reading of that direction reports agreement it never tested.
"""

import itertools

import pytest

from kiro_crew.constants import (
    _MARKER_ACTION_BODY_ATOM,
    _OPTION_ACTIONS_TAIL_PREFIX_RE,
    _OPTIONS_TAIL_PREFIX_RE,
    MARKER_CLOSERS,
    MARKER_OPENERS,
    OPTION_ACTIONS_RE_TRAILER,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)

HEAD = "[OPTION-ACTIONS"
OPTIONS_HEAD = "[OPTIONS"

#: One representative of every class the body rules distinguish: an ordinary char,
#: an ASCII pair, a CJK pair, a MISMATCHED closer, both separators, and both spaces.
ALPHABET = ("x", "[", "]", "\u3010", "\u3011", "\uff3d", "|", ",", " ", "\n")
BODY_LEN = 4
#: Direction one proves a NEGATIVE per refused tail, so it exhausts the candidate list
#: where direction two early-exits; each ALPHABET byte costs tenfold, so it runs shorter.
REFUSAL_BODY_LEN = 3


def accepted(text: str) -> bool:
    """The trailer reads TEXT as a complete marker starting at its first byte."""
    match = OPTION_ACTIONS_RE_TRAILER.search(text)
    return match is not None and match.start() == 0


def held(text: str) -> bool:
    """The probe reads TEXT as a marker still being streamed."""
    return _OPTION_ACTIONS_TAIL_PREFIX_RE.match(text, 0) is not None


def _words(maxlen):
    for length in range(maxlen + 1):
        for combo in itertools.product(ALPHABET, repeat=length):
            yield "".join(combo)


def _corpus(maxlen=BODY_LEN):
    bodies = list(_words(maxlen))
    out = [HEAD + ":close=" + b for b in bodies]
    out += [HEAD + ": close=" + b for b in bodies]
    out += [HEAD + ":" + b for b in bodies]
    return out


CORPUS = _corpus()
MARKERS = [t for t in CORPUS if accepted(t)]
HELD = [t for t in CORPUS if held(t)]
REFUSAL_CORPUS = _corpus(REFUSAL_BODY_LEN)


def _continuations(tail):
    """Candidate continuations, widest-first in the shapes a completion can need.

    Each layer exists because a narrower search reported a false "can never
    finish". A completion must close the tail's own unmatched openers with their
    TYPED partners, and a closer ending a pair needs a following byte that is
    neither a separator nor another closer, so an ordinary byte may have to sit
    between two closers. A uniform run, or a blind sweep short enough to afford,
    generates neither.
    """
    unmatched = [ch for ch in tail if ch in MARKER_OPENERS]
    closers = [MARKER_CLOSERS[MARKER_OPENERS.index(ch)] for ch in reversed(unmatched)]
    out = []
    for mask in range(1 << len(closers)) if len(closers) <= 6 else (0,):
        seq = ""
        for i, closer in enumerate(closers):
            seq += closer
            if mask >> i & 1:
                seq += "x"
        for term in ("",) + tuple(MARKER_CLOSERS):
            out.append(seq + term)
            out.append("x" + seq + term)
    for n in range(1, len(tail) + 3):
        for closer in MARKER_CLOSERS:
            out.append(closer * n)
            out.append("x" + closer * n)
    key = "close="
    for cut in range(len(key) + 1):
        for label in ("x", "x" + "".join(closers), "".join(closers) + "x"):
            out.append(key[cut:] + label + "]")
    for depth in range(1, 4):
        for combo in itertools.product(tuple(MARKER_CLOSERS) + ("x", " "), repeat=depth):
            out.append("".join(combo))
    return out


def completion(tail, accept=accepted):
    """Some continuation making TAIL a complete marker, or None if none is found."""
    if accept(tail):
        return ""
    for cand in _continuations(tail):
        if accept(tail + cand):
            return cand
    return None


class TestTheGeneratedCorpusMeasuresSomething:
    """A vacuous corpus would pass both directions while proving nothing."""

    def test_the_corpus_yields_complete_markers(self):
        assert len(MARKERS) > 100

    def test_the_corpus_yields_streaming_tails(self):
        assert len(HELD) > 100

    def test_the_refusal_corpus_is_not_trivial(self):
        # The cheaper corpus direction one runs over must still hold the shapes
        # every measured defect took: a two-byte body and a three-byte one.
        assert HEAD + ":close=[|" in REFUSAL_CORPUS
        assert HEAD + ":close=x]]" in REFUSAL_CORPUS
        assert len(REFUSAL_CORPUS) > 1000

    def test_the_probe_refuses_ordinary_prose(self):
        assert not held("Answer. [OPTION-ACTIONS is a marker name")

    def test_the_search_can_fail_for_the_intended_reason(self):
        # Negative control: a whitespace-only label can never satisfy the body's
        # nonempty rule, so the search MUST return None for it.
        assert completion(HEAD + ":close=   \u3011") is None

    def test_the_search_finds_a_real_completion(self):
        assert completion(HEAD + ":close=Open") is not None


class TestEveryPrefixOfACompleteMarkerIsHeld:
    """Probe not too strict: a marker mid-stream is never published as raw text.

    This direction runs over ``REFUSAL_CORPUS`` rather than the full one. Its reach
    comes from the completion search, not the corpus: every witness measured so far
    is a two or three byte body (``[|``, ``x]]``), while the held-tail direction
    needs four (``[|]]``). ``test_the_refusal_corpus_is_not_trivial`` pins that.
    """

    def test_no_tail_that_can_still_finish_is_refused(self):
        # Stated over CONTINUATIONS, not over prefixes of markers inside the
        # corpus: a completion may be longer than the corpus can generate.
        refused = []
        for tail in REFUSAL_CORPUS:
            if accepted(tail) or held(tail):
                continue
            if completion(tail) is not None:
                refused.append(tail)
        assert refused == []

    def test_no_streaming_prefix_of_an_accepted_marker_is_refused(self):
        refused = []
        for marker in MARKERS:
            for cut in range(len(HEAD), len(marker)):
                prefix = marker[:cut]
                # A prefix the TRAILER already accepts is not streaming: the
                # splitter detaches it as complete, so the probe never decides it.
                if not accepted(prefix) and not held(prefix):
                    refused.append((marker, prefix))
        assert refused == []

    @pytest.mark.parametrize(
        "tail",
        [
            HEAD + ": close=Open [docs, examples",
            HEAD + ": close=Open [a | b",
            HEAD + ":close=[x|x",
            HEAD + ":close=\u3010x,x",
        ],
    )
    def test_an_open_pair_holding_a_separator_is_held(self, tail):
        assert completion(tail) is not None
        assert held(tail)

    def test_positive_control_a_truncated_marker_is_held_at_every_cut(self):
        marker = HEAD + ": close=Open the docs]"
        cuts = [marker[:i] for i in range(len(HEAD), len(marker))]
        assert all(accepted(c) or held(c) for c in cuts)


class TestEveryHeldTailCanStillFinish:
    """Probe not too loose: nothing is detached that no continuation can complete."""

    def test_no_held_tail_is_impossible_to_complete(self):
        assert [t for t in HELD if completion(t) is None] == []

    def test_no_held_tail_loses_text_it_can_never_justify(self):
        lost = []
        for tail in HELD:
            _, suffix = split_trailing_protocol_suffix("Answer.\n" + tail)
            if suffix and completion(tail) is None:
                lost.append(tail)
        assert lost == []

    @pytest.mark.parametrize(
        "tail",
        [
            HEAD + ":close=[|]]",
            HEAD + ":close=   \u3011",
            HEAD + ": close=Open \u3010docs\uff3d ",
        ],
    )
    def test_a_tail_that_cannot_finish_keeps_its_text(self, tail):
        text = "Answer.\n" + tail
        visible, suffix = split_trailing_protocol_suffix(text)
        assert (visible, suffix) == (text, "")


class TestTheProbeIsBuiltFromTheBodysOwnAtom:
    """The derivation is the guarantee; these pin it against a silent re-spelling."""

    def test_the_probe_pattern_contains_the_shared_atom(self):
        assert _MARKER_ACTION_BODY_ATOM in _OPTION_ACTIONS_TAIL_PREFIX_RE.pattern

    def test_the_trailer_pattern_contains_the_shared_atom(self):
        assert _MARKER_ACTION_BODY_ATOM in OPTION_ACTIONS_RE_TRAILER.pattern


def options_accepted(text: str) -> bool:
    """The OPTIONS trailer reads TEXT as a complete marker from its first byte."""
    match = OPTIONS_RE_TRAILER.search(text)
    return match is not None and match.start() == 0


def options_held(text: str) -> bool:
    """The OPTIONS probe reads TEXT as a marker still being streamed."""
    return _OPTIONS_TAIL_PREFIX_RE.match(text, 0) is not None


def options_completion(tail):
    return completion(tail, options_accepted)


#: Head literals are TOKENS because a nested head is eight characters: a corpus of
#: single characters cannot spell one and would go blind on the temper.
NESTED_HEADS = ("OPTIONS:", "OPTION-ACTIONS:")
OPTIONS_TOKENS = ("x", "|", " ") + tuple(MARKER_OPENERS) + tuple(MARKER_CLOSERS) + NESTED_HEADS
OPTIONS_DEPTH = 3


def _options_corpus():
    out = []
    for n in range(OPTIONS_DEPTH + 1):
        for combo in itertools.product(OPTIONS_TOKENS, repeat=n):
            body = "".join(combo)
            out.append(OPTIONS_HEAD + ":" + body)
            out.append(OPTIONS_HEAD + ": " + body)
    return out


OPTIONS_CORPUS = _options_corpus()
OPTIONS_HELD = [t for t in OPTIONS_CORPUS if options_held(t)]
OPTIONS_MARKERS = [t for t in OPTIONS_CORPUS if options_accepted(t)]


def _body_of(tail: str) -> str:
    return tail[len(OPTIONS_HEAD) + 1 :]


def _nests_a_head_after_an_opener(tail: str) -> bool:
    body = _body_of(tail)
    return any(opener + head in body for opener in MARKER_OPENERS for head in NESTED_HEADS)


class TestTheOptionsCorpusMeasuresSomething:
    """Vacuity controls, including the one the action corpus lacked."""

    def test_the_corpus_can_spell_a_nested_head_after_every_opener(self):
        """Without this, the corpus silently cannot reach the class under test."""
        for opener in MARKER_OPENERS:
            for head in NESTED_HEADS:
                assert OPTIONS_HEAD + ":" + opener + head in OPTIONS_CORPUS

    def test_the_corpus_yields_complete_markers_and_streaming_tails(self):
        assert len(OPTIONS_MARKERS) > 50
        assert len(OPTIONS_HELD) > 500

    def test_the_search_finds_a_real_completion(self):
        assert options_completion(OPTIONS_HEAD + ": Yes") is not None

    def test_the_search_can_fail_for_the_intended_reason(self):
        assert options_completion(OPTIONS_HEAD + ":\u3011x") is None


class TestNoOptionsTailThatCanStillFinishIsRefused:
    """Probe not too strict: a marker mid-stream is never published as raw text."""

    def test_no_refused_tail_has_a_continuation_that_completes_it(self):
        refused = [
            t
            for t in OPTIONS_CORPUS
            if not options_accepted(t) and not options_held(t) and options_completion(t) is not None
        ]
        assert refused == []


class TestTheOptionsTemperAgreesWithTheBody:
    """A nested head already in the buffer can never be rescued by a continuation.

    This is the HALF of probe/trailer agreement that holds for the OPTIONS pair.
    Full agreement does NOT: the probe is deliberately looser about a lookalike
    CLOSER mid-label, because the unfinished check is ASCII-only on purpose and a
    shipped regression test locks that -- widening it was blocked once already.
    The temper is different in kind. A closer may be rescued by the next byte; a
    nested head sits at a position already received, and the body refuses it after
    EVERY opener, so no continuation can make it legal.
    """

    def test_every_tail_nesting_a_head_after_an_opener_is_refused(self):
        """Stated POSITIVELY, over the tails themselves.

        The natural phrasing -- "no HELD tail nests a head it cannot complete" --
        is vacuous once the probe is correct: the fix removes every such tail from
        the held set, so the filter finds none because its precondition is
        unreachable, not because the property holds. The length assertion is the
        working-set control that keeps this measuring something.
        """
        nesting = [t for t in OPTIONS_CORPUS if _nests_a_head_after_an_opener(t)]
        assert len(nesting) > 100
        assert [t for t in nesting if options_held(t)] == []
        assert [t for t in nesting if options_completion(t) is not None] == []

    @pytest.mark.parametrize("opener", tuple(MARKER_OPENERS))
    @pytest.mark.parametrize("head", NESTED_HEADS)
    def test_a_nested_head_after_any_opener_strands_no_text(self, opener, head):
        """Nothing is detached that no continuation can justify.

        Not "nothing is detached": a nested ASCII head IS a sentinel, so the
        rightmost-first walk finds the INNER marker and detaches that, which is
        correct and completable. A lookalike opener spells no sentinel, so the
        whole tail stays visible. Both satisfy the one property that matters.
        """
        text = "Answer.\n" + OPTIONS_HEAD + ": see " + opener + head + " x"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible + suffix == text
        if suffix:
            assert options_completion(suffix) is not None

    def test_the_lookalike_closer_looseness_survives(self):
        """Negative control: tightening the probe this far would break shipped behaviour."""
        for close in "\u3011\uff3d\u3015":
            text = f"body prose [OPTIONS: Use {close} the bracket"
            visible, suffix = split_trailing_protocol_suffix(text)
            assert visible == "body prose "
            assert suffix == f"[OPTIONS: Use {close} the bracket"

    def test_an_ordinary_nested_bracket_is_still_held(self):
        tail = OPTIONS_HEAD + ": see \u3010docs\u3011 "
        assert options_held(tail)
        assert options_completion(tail) is not None
