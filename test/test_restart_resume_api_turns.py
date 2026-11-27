"""Which running turns a planned restart records for the next boot to resume.

A one-request ``POST /v1/responses`` turn is left out: its caller has gone, and
resuming it would run tools nobody waits for. Every other running turn is recorded,
the frozen ``/v1/chat/completions`` route's one-request sessions included.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import crew_api_responses, restart_resume


def _slot(*, running: bool = True, exempt: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        _turn_in_flight_generation=1 if running else 0, _dashboard_card_exempt=exempt
    )


@pytest.mark.asyncio
async def test_a_planned_restart_records_every_running_turn_but_a_one_request_api_turn(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(restart_resume, "config_dir", lambda: tmp_path)
    state = SimpleNamespace(
        _slots={
            "chat-1": _slot(),
            "idle": _slot(running=False),
            # The Crew API's one-request session, named and marked by its route.
            "resp-0123abcd": _slot(exempt=True),
            # The frozen route's one-request session.
            "oai-chatcmpl-1": _slot(exempt=True),
            # A person's own chat that happens to carry the prefix.
            "resp-notes": _slot(),
        }
    )
    await restart_resume.record_running_slots(state)
    recorded = json.loads(restart_resume.intent_path().read_text(encoding="utf-8"))
    assert recorded == ["chat-1", "oai-chatcmpl-1", "resp-notes"]


def test_the_prefix_is_the_one_the_responses_route_names_its_session_with():
    source = crew_api_responses.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert f'get_or_create_slot("{restart_resume.ONE_REQUEST_API_PREFIX}" +' in text
