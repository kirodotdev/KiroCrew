"""The streaming probe and the complete ``[OPTIONS:]`` grammar agree on nested heads.

``split_trailing_protocol_suffix`` asks two patterns about a reply's tail: one
decides "is this a finished marker?", the other "could this still become one?".
A detached suffix is discarded rather than rendered, so a tail the probe holds
and the body can never accept loses its text with no error.

The two do NOT agree in general, by design. The probe is deliberately looser
about a lookalike CLOSER mid-label, which a shipped regression test locks. A
nested HEAD is different in kind: it sits at a position already received, and
the body refuses it after every opener, so no continuation can make it legal.
That half is asserted here.

The corpus is built from TOKENS, not characters: a nested head is eight
characters, so a single-character alphabet could not spell one.
"""

import itertools

import pytest

from kiro_crew.constants import (
    _OPTIONS_TAIL_PREFIX_RE,
    MARKER_CLOSERS,
    MARKER_OPENERS,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)

HEAD = "[OPTIONS"
NESTED_HEAD = "OPTIONS:"
#: Brackets come from the grammar's own sets, so a newly added pair cannot leave
#: this corpus blind to it.
TOKENS = ("x", "|", " ") + tuple(MARKER_OPENERS) + tuple(MARKER_CLOSERS) + (NESTED_HEAD,)
DEPTH = 3


def accepted(text: str) -> bool:
    """The trailer reads TEXT as a complete marker starting at its first byte."""
    match = OPTIONS_RE_TRAILER.search(text)
    return match is not None and match.start() == 0


def held(text: str) -> bool:
    """The probe reads TEXT as a marker still being streamed."""
    return _OPTIONS_TAIL_PREFIX_RE.match(text, 0) is not None


def _corpus():
    out = []
    for length in range(DEPTH + 1):
        for combo in itertools.product(TOKENS, repeat=length):
            body = "".join(combo)
            out.append(HEAD + ":" + body)
            out.append(HEAD + ": " + body)
    return out


CORPUS = _corpus()
HELD = [t for t in CORPUS if held(t)]
MARKERS = [t for t in CORPUS if accepted(t)]


def _continuations(tail):
    """Candidate continuations, in the shapes a completion can need.

    A completion must close the tail's own unmatched openers with their TYPED
    partners, and a closer ending a pair needs a following byte that is neither a
    separator nor another closer, so an ordinary byte may have to sit between two
    closers. A uniform run generates neither.
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
    for depth in range(1, 4):
        for combo in itertools.product(tuple(MARKER_CLOSERS) + ("x", " ", "|"), repeat=depth):
            out.append("".join(combo))
    return out


def completion(tail):
    """Some continuation making TAIL a complete marker, or None if none is found."""
    if accepted(tail):
        return ""
    for cand in _continuations(tail):
        if accepted(tail + cand):
            return cand
    return None


def _body_of(tail: str) -> str:
    return tail[len(HEAD) + 1 :]


def _nests_a_head_after_an_opener(tail: str) -> bool:
    return any(opener + NESTED_HEAD in _body_of(tail) for opener in MARKER_OPENERS)


class TestTheCorpusMeasuresSomething:
    """A vacuous corpus would pass everything below while proving nothing."""

    def test_the_corpus_can_spell_a_nested_head_after_every_opener(self):
        """Without this, the corpus silently cannot reach the class under test."""
        for opener in MARKER_OPENERS:
            assert HEAD + ":" + opener + NESTED_HEAD in CORPUS

    def test_the_corpus_yields_complete_markers_and_streaming_tails(self):
        assert len(MARKERS) > 50
        assert len(HELD) > 500

    def test_the_search_finds_a_real_completion(self):
        assert completion(HEAD + ": Yes") is not None

    def test_the_search_can_fail_for_the_intended_reason(self):
        assert completion(HEAD + ":\u3011x") is None

    def test_the_probe_refuses_a_diverging_tail(self):
        assert not held("prose [OPTIONSDOC")


class TestNoTailThatCanStillFinishIsRefused:
    """Probe not too strict: a marker mid-stream is never published as raw text."""

    def test_no_refused_tail_has_a_continuation_that_completes_it(self):
        refused = [
            t for t in CORPUS if not accepted(t) and not held(t) and completion(t) is not None
        ]
        assert refused == []


class TestTheNestedHeadGuardAgreesWithTheBody:
    """A nested head already in the buffer can never be rescued by a continuation."""

    def test_every_tail_nesting_a_head_after_an_opener_is_refused(self):
        """Stated POSITIVELY, over the tails themselves.

        The natural phrasing -- "no HELD tail nests a head it cannot complete" --
        is vacuous once the probe is correct: the fix removes every such tail from
        the held set, so the filter finds none because its precondition is
        unreachable, not because the property holds. The length assertion is the
        working-set control that keeps this measuring something.
        """
        nesting = [t for t in CORPUS if _nests_a_head_after_an_opener(t)]
        assert len(nesting) > 100
        assert [t for t in nesting if held(t)] == []
        assert [t for t in nesting if completion(t) is not None] == []

    @pytest.mark.parametrize("opener", tuple(MARKER_OPENERS))
    def test_a_nested_head_after_any_opener_strands_no_text(self, opener):
        """Nothing is detached that no continuation can justify.

        Not "nothing is detached": a nested ASCII head IS a sentinel, so the
        rightmost-first walk finds the INNER marker and detaches that, which is
        correct and completable. A lookalike opener spells no sentinel, so the
        whole tail stays visible. Both satisfy the one property that matters.
        """
        text = "Answer.\n" + HEAD + ": see " + opener + NESTED_HEAD + " x"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible + suffix == text
        if suffix:
            assert completion(suffix) is not None

    def test_the_lookalike_closer_looseness_survives(self):
        """Negative control: tightening the probe this far would break shipped behaviour."""
        for close in "\u3011\uff3d\u3015":
            text = f"body prose [OPTIONS: Use {close} the bracket"
            visible, suffix = split_trailing_protocol_suffix(text)
            assert visible == "body prose "
            assert suffix == f"[OPTIONS: Use {close} the bracket"

    def test_an_ordinary_nested_bracket_is_still_held(self):
        tail = HEAD + ": see \u3010docs\u3011 "
        assert held(tail)
        assert completion(tail) is not None
