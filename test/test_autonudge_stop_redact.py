"""A stop record's text is scrubbed of credentials before it is clipped."""

from __future__ import annotations

import pytest

from kiro_crew import autonudge_stop_log as stoplog

# Obviously fake GitHub classic PAT shape, split so no scanner sees a literal.
_FAKE_PAT = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"


@pytest.mark.parametrize("limit", [stoplog.FIELD_MAX_CHARS, stoplog.DETAIL_MAX_CHARS])
def test_safe_text_hides_a_secret_cut_by_the_clip(limit):
    # The clip lands in the middle of the fake secret.
    text = "x" * (limit - 10) + " " + _FAKE_PAT + " tail"
    out = stoplog.safe_text(text, limit)
    assert len(out) <= limit
    assert "ghp_" not in out
    assert _FAKE_PAT[4:9] not in out
