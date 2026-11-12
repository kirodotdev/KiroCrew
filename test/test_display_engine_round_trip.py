"""An engine change during one display render keeps that render's answer out of every cache.

The owner's engine PUT can move the selector structured -> legacy -> structured while a
render runs. The engine mark is the same before and after such a round trip, so a cache
key and a before/after mark comparison cannot see it, and an answer the legacy engine
gave would be kept under the structured key. Each test makes the battery itself do the
round trip (no threads, no sleeps) and then checks the next render runs the battery
again instead of serving that answer.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from redaction_structured_cases import github_classic

from kiro_crew.dashboard import chat_utils_display as cud
from kiro_crew.dashboard import wire_render
from kiro_crew.security.redaction_structured import compat

SECRET = github_classic(seed=152)


@pytest.fixture(autouse=True)
def _structured() -> Iterator[None]:
    before = compat.use_rule_table()
    compat.publish_use_rule_table(True)
    cud._clear_display_redaction_cache()
    wire_render.clear_render_memo()
    yield
    cud._clear_display_redaction_cache()
    wire_render.clear_render_memo()
    compat.publish_use_rule_table(before)


@pytest.fixture
def round_trip(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The battery flips the engine to legacy and back once, answering 'clean' meanwhile."""
    real = cud.display_battery
    state = {"left": 1, "calls": 0}

    def battery(text, *args, **kwargs):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["left"]:
            state["left"] -= 1
            compat.publish_use_rule_table(False)
            compat.publish_use_rule_table(True)
            return text
        return real(text, *args, **kwargs)

    monkeypatch.setattr(cud, "display_battery", battery)
    return state


def test_a_round_trip_during_one_string_keeps_its_answer_out_of_the_caches(round_trip):
    text = f"token {SECRET} end"
    assert cud._redact_for_display(text) == text
    assert SECRET not in cud._redact_for_display(text)
    assert round_trip["calls"] == 2


def test_a_round_trip_during_one_preview_keeps_it_out_of_the_preview_caches(round_trip):
    text = f"token {SECRET} end"
    first = cud._redacted_preview(text, 80)
    assert SECRET in first
    assert SECRET not in cud._redacted_preview(text, 80)


def test_a_round_trip_during_one_render_keeps_its_rows_out_of_the_memo(round_trip):
    rows = [{"role": "assistant", "content": f"token {SECRET} end", "ts": "t0"}]
    first = wire_render.prepare_messages_json(rows, False, live_child="", workspace=None)
    # The render sees the publish and renders its rows again against the live engine.
    assert SECRET not in first
    second = wire_render.prepare_messages_json(rows, False, live_child="", workspace=None)
    assert SECRET not in second
