"""The structured engine's merge steps stay linear in the number of hits and values.

A tool result with many ``token=`` values, each led by a detector hit, once made
:func:`compat._token_param_gaps` compare every value with every hit, and
:func:`compat._bearer_gaps` and :func:`compat._joined` did the same: about 9 s for a
954 KB result, on the gateway loop. These tests count the work instead of timing
it, so a slow runner cannot make them flaky.
"""

from __future__ import annotations

from kiro_crew.security import redaction
from kiro_crew.security.redaction_structured import compat
from kiro_crew.security.redaction_structured.model import Hit
from kiro_crew.testing.synthetic_credentials import github_classic

_N = 2_000
#: Generous: the work is a few steps per hit and per value. The quadratic code
#: does about ``_N * _N / 2`` (2,000,000) here.
_BOUND = 50 * _N


class _CountingList(list):
    """A list that counts the items read from it, by iteration or membership test."""

    reads = 0

    def __iter__(self):
        for item in super().__iter__():
            self.reads += 1
            yield item

    def __contains__(self, item: object) -> bool:
        self.reads += len(self)
        return super().__contains__(item)


def _hit(start: int, end: int) -> Hit:
    return Hit(start, end, "github_classic", "credential", "github", "checksum")


def _token_values(n: int) -> tuple[str, list[Hit]]:
    """``n`` lines of ``?token=<token>.tail``, with a hit over each token."""
    tok = github_classic(seed=5)
    parts: list[str] = []
    hits: list[Hit] = []
    pos = 0
    for i in range(n):
        head = f"https://h.example/p{i}?token="
        parts.append(f"{head}{tok}.tail\n")
        hits.append(_hit(pos + len(head), pos + len(head) + len(tok)))
        pos += len(parts[-1])
    return "".join(parts), hits


def _bearer_values(n: int) -> tuple[str, list[Hit]]:
    """``n`` lines of ``Authorization: Bearer <token>~tail``, with a hit over each token."""
    tok = github_classic(seed=6)
    parts: list[str] = []
    hits: list[Hit] = []
    pos = 0
    for _i in range(n):
        head = "Authorization: Bearer "
        parts.append(f"{head}{tok}~tail\n")
        hits.append(_hit(pos + len(head), pos + len(head) + len(tok)))
        pos += len(parts[-1])
    return "".join(parts), hits


def _spans(hits: list[Hit]) -> list[tuple[int, int, str]]:
    return [(h.start, h.end, redaction.REDACTED_CREDENTIAL_TAG) for h in hits]


def test_token_param_gaps_reads_each_hit_a_bounded_number_of_times() -> None:
    text, hits = _token_values(_N)
    counted = _CountingList(hits)
    gaps, warnings = compat._token_param_gaps(text, counted, _spans(hits))
    assert len(gaps) == _N and len(warnings) == _N
    assert counted.reads <= _BOUND, counted.reads


def test_bearer_gaps_reads_each_hit_a_bounded_number_of_times() -> None:
    text, hits = _bearer_values(_N)
    counted = _CountingList(hits)
    gaps = compat._bearer_gaps(text, counted, _spans(hits))
    assert len(gaps) == _N
    assert counted.reads <= _BOUND, counted.reads


def test_joined_reads_each_extra_span_a_bounded_number_of_times() -> None:
    spans = [(i * 10, i * 10 + 5, "t") for i in range(_N)]
    extra = _CountingList((i * 10 + 5, i * 10 + 8, "t") for i in range(_N))
    joined = compat._joined(spans, extra)
    assert joined == [(i * 10, i * 10 + 8, "t") for i in range(_N)]
    assert extra.reads <= _BOUND, extra.reads


def test_token_param_gaps_still_finds_only_values_a_hit_touches() -> None:
    """The fast lookup keeps the overlap rule: a value no hit touches is left to the passes."""
    text, hits = _token_values(3)
    kept = [hits[0], hits[2]]
    gaps, _w = compat._token_param_gaps(text, kept, _spans(kept))
    starts = {s for s, _e, _t in gaps}
    assert starts == {hits[0].end, hits[2].end}
