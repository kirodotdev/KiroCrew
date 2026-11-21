"""The backend half of the hidden-reply pin: one verdict per fixture row.

``kiro_crew.dashboard.hidden_reply.is_hidden_reply_at`` is the twin of the
dashboard's ``isHiddenReplyAt``; ``website/src/test/hiddenReplyTwin.test.ts`` checks
the same rows against the TypeScript. A row whose verdict differs between the two
is a Regenerate that targets a row the dashboard does not show.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.dashboard.hidden_reply import is_hidden_reply_at, turn_had_policy_block

FIXTURE = Path(__file__).parent / "fixtures" / "hidden_reply_rows.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_the_fixture_has_enough_cases_to_mean_something() -> None:
    assert len(CASES) >= 21
    assert all(len(c["rows"]) == len(c["hidden"]) for c in CASES)
    # Both verdicts occur on the indexed rows, so an all-false predicate fails.
    assert {c["hidden"][-1] for c in CASES} == {True, False}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_every_row_matches_the_fixture_verdict(case: dict) -> None:
    rows = case["rows"]
    assert [is_hidden_reply_at(rows, i) for i in range(len(rows))] == case["hidden"]


def test_a_row_without_content_stays_a_candidate() -> None:
    # The wire leaves the key out and the dashboard cannot judge such a row.
    rows = [{"role": "user", "content": "plan"}, {"role": "assistant"}]
    assert is_hidden_reply_at(rows, 1) is False


@pytest.mark.parametrize(
    ("steer", "steered"),
    [
        (True, True),
        ({}, True),
        ([], True),
        ("x", True),
        (1, True),
        (False, False),
        (0, False),
        ("", False),
        (None, False),
    ],
)
def test_the_steer_flag_is_read_with_javascript_truthiness(steer: object, steered: bool) -> None:
    rows = [
        {"role": "user", "content": "plan", "meta": {"steer": steer}},
        {"role": "inject", "content": "[Tool blocked — reason sent to the agent] denied"},
        {"role": "assistant", "content": "[STEERING steer-0a: ok]"},
    ]
    assert turn_had_policy_block(rows, 2) is (not steered)
