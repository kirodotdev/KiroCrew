"""The stop-reason block lives in ``acp/types_stop.py``; ``acp/types.py`` stays its import path."""

from __future__ import annotations

import pytest

from kiro_crew.acp import types, types_stop

_MOVED = (
    "STOP_CLASS_CANCELLED",
    "STOP_CLASS_FAILED",
    "STOP_CLASS_RECOVERING",
    "STOP_CLASS_STALLED",
    "STOP_CLASS_SUCCEEDED",
    "STOP_REASON_CANCELLED",
    "STOP_REASON_CANCEL_UNACKED",
    "STOP_REASON_COMPACTION_FAILED",
    "STOP_REASON_CONTENT_FILTERED_WIRE",
    "STOP_REASON_END_TURN",
    "STOP_REASON_REFUSAL",
    "STOP_REASON_STALE_RECOVER",
    "STOP_REASON_TIMEOUT",
    "STOP_REASON_TOOL_STALL",
    "STOP_RECOVERY_MAX_RETRIES",
    "StopClass",
    "_STOP_REASON_ERROR_PREFIX",
    "classify_stop_reason",
)


@pytest.mark.parametrize("name", _MOVED)
def test_types_re_exports_the_same_object(name: str) -> None:
    assert getattr(types, name) is getattr(types_stop, name)
