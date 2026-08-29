"""Backend half of the cross-language marker-grammar parity pin.

The grammar is hand-mirrored: ``constants.py`` and ``optionMarker.ts`` each re-derive
the tempered body, the sibling lookahead and the tail. Nothing made them agree, and the
pair has already drifted once in a way that shipped -- the ``[OPTIONS:]`` head is
case-sensitive here and case-insensitive on the frontend, which is why per-head casing
machinery had to be added rather than inherited.

Parallel hand-written suites structurally cannot catch that: each side only ever asks
its own implementation what it thinks. This reads ONE shared corpus and asserts the
backend's answers match the recorded contract; the vitest half asserts the frontend's
answers match the same file. Disagreement between the two implementations therefore
fails on whichever side diverged, rather than being invisible.

The corpus lives beside ``options.ts`` because that directory is already the
cross-language authority this suite reads from -- see
``TestTheTwoLanguagesAgreeOnTheActionEnum``, which extracts ``KNOWN_ACTIONS`` from the
frontend source rather than restating it.
"""

import json
import pathlib

import pytest

from kiro_crew.constants import OPTIONS_RE_LINE, strip_action_markers
from kiro_crew.dashboard.state import _has_option_actions, _parse_options

CORPUS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "app-sdk"
    / "protocol"
    / "markerCorpus.json"
)


def _strip_both_heads(text: str) -> str:
    """Remove both markers, mirroring the renderer strip at ``slack/handler.py``.

    Deliberately the same composition (content head, then the guarded action strip,
    then trim) rather than a fresh one: a strip invented for the test could pass while
    the shipped one drifts. Actions go through ``strip_action_markers`` because that is
    what ships -- the raw pattern would excise a span nested in an unclosed marker.
    """
    return strip_action_markers(OPTIONS_RE_LINE.sub("", text)).strip()


def _cases() -> list[dict]:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    return data["cases"]


def _expected(case: dict, side: str) -> dict:
    """The trio this side must produce, honouring a recorded divergence.

    A divergent case is not a licence to skip: it still asserts an exact expectation,
    just this side's one. ``_side_expectations_differ`` is what keeps that honest.
    """
    if "divergent" in case:
        return case["divergent"][side]
    return case


def test_the_corpus_is_readable_and_populated():
    """Positive control, so every parametrised assertion below cannot pass vacuously.

    A renamed or moved corpus would otherwise yield an EMPTY case list, and pytest
    reports zero collected cases as success for the file. That is the same
    false-all-clear this pin exists to prevent, one level up.
    """
    cases = _cases()
    assert len(cases) >= 8, f"corpus looks truncated: {len(cases)} case(s)"
    for case in cases:
        for side in ("backend", "frontend"):
            for field in ("options", "hasAction", "stripped"):
                assert field in _expected(
                    case, side
                ), f"case {case.get('name')!r} is missing {field!r} for {side}"
        assert "name" in case and "text" in case
    names = [c["name"] for c in cases]
    assert len(set(names)) == len(names), "duplicate case names"
    # The divergence the pin exists for must actually be represented.
    assert any("[option-actions:" in c["text"] for c in cases), "no lowercase action head"
    assert any("[options:" in c["text"] for c in cases), "no lowercase content head"


def test_side_expectations_differ_on_every_divergent_case():
    """A recorded divergence must still BE one.

    Without this, resolving the drift on either side would leave the corpus asserting a
    disagreement that no longer exists -- a stale claim that reads as a live finding.
    Making the two halves equal fails here and forces the case back to the shared shape.
    """
    divergent = [c for c in _cases() if "divergent" in c]
    assert divergent, "no divergent case: the casing drift this pin was built for is unrepresented"
    for case in divergent:
        back, front = case["divergent"]["backend"], case["divergent"]["frontend"]
        assert back != front, (
            f"{case['name']!r} is marked divergent but both sides expect the same thing; "
            "collapse it to shared options/hasAction/stripped fields"
        )


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_backend_matches_the_shared_corpus(case):
    want = _expected(case, "backend")
    assert _parse_options(case["text"]) == want["options"], "options"
    assert _has_option_actions(case["text"]) is want["hasAction"], "hasAction"
    assert _strip_both_heads(case["text"]) == want["stripped"], "stripped"
