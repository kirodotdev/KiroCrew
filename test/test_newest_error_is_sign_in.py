"""``newest_error_is_sign_in``: the sidebar row of a chat whose last turn ended on a sign-in error.

The row reads "Turn interrupted · Sign in, then Resume" when the slot summary
carries ``interrupted_by_sign_in``. The composer answers the same question in the
browser (``authRequiredIsNewest``), so both are held to one shared table.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.chat_utils_injected_rows import AUTH_REQUIRED_KIND
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.state_parts import transcript_rows

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "sign_in_newest_rows.json").read_text(encoding="utf-8")
)["cases"]

_SIGN_IN_ERROR = {"role": "error", "content": "Not signed in.", "meta": {"kind": "auth_required"}}


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_answers_the_shared_table(case: dict) -> None:
    assert transcript_rows.newest_error_is_sign_in(case["rows"]) is case["expected"]


def test_reads_the_kind_chat_utils_stamps() -> None:
    assert transcript_rows._AUTH_REQUIRED_KIND == AUTH_REQUIRED_KIND


def test_the_slot_summary_carries_it_beside_interrupted() -> None:
    slot = _ChatSlot("chat-1")
    slot.messages = [{"role": "user", "content": "hello"}, dict(_SIGN_IN_ERROR)]
    row = slot.to_dict()
    assert row["interrupted"] is True
    assert row["interrupted_by_sign_in"] is True


def test_a_running_turn_is_never_flagged() -> None:
    # The sign-in error is still the newest row, but a turn is running again.
    slot = _ChatSlot("chat-1")
    slot.messages = [{"role": "user", "content": "hello"}, dict(_SIGN_IN_ERROR)]
    slot.task = SimpleNamespace(done=lambda: False)
    row = slot.to_dict()
    assert row["interrupted"] is False
    assert row["interrupted_by_sign_in"] is False
