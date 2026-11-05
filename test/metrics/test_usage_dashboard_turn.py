"""The usage metrics a real dashboard turn records, read off the turn itself.

Drives one landed dashboard turn through ``turn_harness.run_turn`` (the
production ``_run_chat``) and asserts on what it recorded, so the three
dashboard wirings are pinned by behaviour rather than by reading the turn's
source:

* the finished turn is counted on ``kirocrew.turn.completed`` with a closed
  ``trigger_source``;
* the turn entry declares that trigger for the session the turn opens
  (``usage_attrs.current_trigger`` inside the turn);
* the admitted turn's composition records ``kirocrew.memory.recalled``
  ``{path=context}`` exactly once.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from turn_harness import Do, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.metrics import feature_usage as fu
from kiro_crew.metrics import turn_completed as tc
from kiro_crew.metrics.usage_attrs import TRIGGER_SOURCE_VALUES, current_trigger


class _Recorder:
    def __init__(self) -> None:
        self.counters: list[dict] = []

    def counter(self, name, value=1, *, unit="1", attrs=None, **kw) -> None:
        self.counters.append({"name": name, "attrs": dict(attrs or {})})

    def histogram(self, *a, **kw) -> None:
        return None


async def _landed_turn():
    rec = _Recorder()
    emitted: list[tuple[str, dict]] = []
    seen_trigger: list[str | None] = []
    script = TurnScript(
        events=[
            Do(lambda ctx: seen_trigger.append(current_trigger())),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    with (
        patch.object(tc, "get_recorder", return_value=rec),
        patch.object(fu, "_emit", side_effect=lambda name, attrs: emitted.append((name, attrs))),
    ):
        record = await run_turn(script)
    return rec, emitted, seen_trigger, record


@pytest.mark.asyncio
async def test_a_dashboard_turn_records_its_usage_metrics():
    rec, emitted, seen_trigger, record = await _landed_turn()
    assert record.stop_reason == STOP_REASON_END_TURN

    completed = [c for c in rec.counters if c["name"] == tc.TURN_COMPLETED_METRIC]
    assert len(completed) == 1
    attrs = completed[0]["attrs"]
    assert attrs["trigger_source"] in TRIGGER_SOURCE_VALUES
    assert attrs["origin"] == "dashboard"

    # The entry declared the same trigger for anything the turn opens.
    assert seen_trigger == [attrs["trigger_source"]]

    recalls = [a for name, a in emitted if name == fu.MEMORY_RECALLED]
    assert [a["path"] for a in recalls] == ["context"]
