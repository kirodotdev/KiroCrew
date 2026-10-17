"""The server's ``references_remote_asset`` answers as the player's does.

The import warns on a Lottie clip the player refuses for fetching, so the two
predicates must agree. ``website/src/test/lottieSafety.test.ts`` runs
``referencesRemoteAsset`` over the same fixture file this module reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.appearance_packs.lottie_safety import (
    references_remote_asset,
    text_references_remote_asset,
)

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "website/src/test/fixtures/lottieRemoteAssetCases.json"
)
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_server_agrees_with_the_player(case):
    assert references_remote_asset(case["doc"]) is case["remote"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_text_entry_point_parses_the_way_the_player_does(case):
    assert text_references_remote_asset(json.dumps(case["doc"])) is case["remote"]


def test_the_fixture_pins_both_answers():
    """A fixture of one answer only would pass a predicate that always says it."""
    assert {c["remote"] for c in _CASES} == {True, False}


@pytest.mark.parametrize(
    "text",
    ["", "not json", "\ufeff{}", '{"assets": [{"p": "a.png"}], "fr": Infinity}', "[" * 100_000],
    ids=["empty", "not-json", "bom", "infinity", "deep-unclosed"],
)
def test_text_the_player_cannot_parse_is_not_called_remote(text):
    assert text_references_remote_asset(text) is False
