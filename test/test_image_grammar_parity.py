"""The markdown image grammar the dashboard ports must match the backend's.

``website/src/lib/imageArtifactSlug.ts`` re-implements :data:`IMAGE_MD_RE` and
:func:`md_destination` so the dashboard can find a chat image's durable artifact
by its ordinal in the raw message.  Both suites consume
``test/fixtures/image_grammar_parity.json``; a grammar change on either side
that is not mirrored fails here or in ``imageGrammarParity.test.ts``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.messaging.outbound_files import IMAGE_MD_RE, md_destination

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "image_grammar_parity.json"
_CORPUS = json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CORPUS["destinations"], ids=lambda c: repr(c["rest"]))
def test_destination_parity(case: dict) -> None:
    assert md_destination(case["rest"]) == case["expect"]


@pytest.mark.parametrize("case", _CORPUS["openers"], ids=lambda c: repr(c["text"]))
def test_opener_count_parity(case: dict) -> None:
    assert len(IMAGE_MD_RE.findall(case["text"])) == case["count"]
