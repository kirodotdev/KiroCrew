"""An agent's stop of a legacy loop keeps the row (``agent_stop``).

``autonudge_stop`` / ``monitor_stop`` on an ordinary legacy loop deactivate it in
place with the agent's redacted reason in ``stopped_detail``, so the popover and
``monitor_inspect`` can say why it ended; the agent's next arm on the slot
replaces the row, as it replaces a spent bound.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge import (
    AGENT_STOP_REASON,
    MAX_MONITOR_STOP_REASON_CHARS,
    AutoNudgeService,
)
from kiro_crew.autonudge_stop_log import stop_records
from kiro_crew.dashboard.handlers.autonudge import _autonudge_loop_reading
from kiro_crew.dashboard.session_directive_apply import apply_session_directive


@pytest.fixture
def service(tmp_path_factory):
    svc = AutoNudgeService(base_dir=tmp_path_factory.mktemp("autonudge-store"))
    yield svc
    svc.stop()


async def _agent_stops(service: AutoNudgeService, kind: str, reason: str) -> str:
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
    ):
        return await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            kind,
            {"reason": reason},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["autonudge_stop", "monitor_stop"])
async def test_an_agent_stop_keeps_the_row_with_its_reason(service, kind) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)

    result = await _agent_stops(service, kind, "PR merged, nothing left to do")

    assert "stopped" in result and "NOT" not in result
    kept = service.get_by_slot("chat-1")
    assert kept is not None and kept.id == loop.id
    assert kept.active is False
    assert kept.stopped_reason == AGENT_STOP_REASON
    assert kept.stopped_detail == "PR merged, nothing left to do"
    stored = json.loads(service._store.path.read_text())["loops"][0]
    assert stored["stopped_reason"] == AGENT_STOP_REASON
    assert stored["stopped_detail"] == "PR merged, nothing left to do"


@pytest.mark.asyncio
async def test_monitor_inspect_says_why_the_agent_stopped_it(service) -> None:
    await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "blocked on a maintainer ruling")

    reading = _autonudge_loop_reading(service.get_by_slot("chat-1"))

    assert reading["active"] is False
    assert reading["stopped_reason"] == AGENT_STOP_REASON
    assert reading["stopped_detail"] == "blocked on a maintainer ruling"


@pytest.mark.asyncio
async def test_the_agents_next_arm_replaces_the_stopped_row(service) -> None:
    first = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "done")

    # The directive re-arm's own flags: create-only, but a stopped row may go.
    second = await service.add(
        slot_key="chat-1",
        message="watch the next one",
        idle_secs=300,
        replace_existing=False,
        replace_stopped=True,
    )

    assert second.id != first.id
    current = service.get_by_slot("chat-1")
    assert current is not None and current.id == second.id and current.active is True
    assert current.stopped_reason == "" and current.stopped_detail == ""


@pytest.mark.asyncio
async def test_a_revival_clears_the_reason_and_its_words(service) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "done")

    revived = await service.update(loop.id, active=True)

    assert revived is not None and revived.active is True
    assert revived.stopped_reason == "" and revived.stopped_detail == ""


@pytest.mark.asyncio
async def test_update_exposes_each_activity_edge_only_while_observers_run(service) -> None:
    """The existing ``updated`` event carries exact transition data out-of-band."""
    transitions: list[tuple[bool, bool, str] | None] = []

    def _observe(event, loop) -> None:
        if event == "updated" and loop is not None:
            transitions.append(service._activity_transition_for_observer(loop.id))

    service.subscribe(_observe)
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "done")
    await service.update(loop.id, active=True)
    await service.update(loop.id, active=True)

    assert transitions == [
        (True, False, ""),
        (False, True, AGENT_STOP_REASON),
        (True, True, ""),
    ]
    assert service._activity_transition_for_observer(loop.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "resumed_at"), [(3, 3), (24, 0)], ids=["cycles-left", "cap-spent"]
)
async def test_a_persons_resume_of_an_agent_stop_keeps_the_count_unless_the_cap_is_spent(
    service, count, resumed_at
) -> None:
    """The popover's Play is ``update(active=True, fresh_run=True)``: the agent's
    stop is not a finished reason, so the loop revives, and only a spent cap
    restarts its count."""
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300, max_cycles=24)
    loop.cycle_count = count
    await _agent_stops(service, "autonudge_stop", "gave up on a blocker")

    revived = await service.update(loop.id, active=True, fresh_run=True)

    assert revived is not None and revived.active is True
    assert revived.cycle_count == resumed_at
    assert revived.stopped_reason == "" and revived.stopped_detail == ""


@pytest.mark.asyncio
async def test_a_later_reasonless_pause_keeps_the_agents_stop(service) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "done")

    await service.update(loop.id, active=False)

    kept = service.get_by_slot("chat-1")
    assert kept is not None
    assert kept.stopped_reason == AGENT_STOP_REASON and kept.stopped_detail == "done"


@pytest.mark.asyncio
async def test_a_credential_in_the_reason_is_redacted(service) -> None:
    await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "token AKIAIOSFODNN7EXAMPLE leaked")

    kept = service.get_by_slot("chat-1")
    assert kept is not None and "AKIAIOSFODNN7EXAMPLE" not in kept.stopped_detail


@pytest.mark.asyncio
async def test_a_credential_only_the_platform_policy_knows_is_redacted_on_the_stop(
    service, monkeypatch
) -> None:
    """A companion can extend the platform credential policy past the baseline
    redactor; the row, which the state frame broadcasts, is cleaned under it."""
    from kiro_crew import autonudge_stop_log

    def _policy(text: str) -> str:
        return text.replace("corp-key-0042", "[REDACTED]")

    monkeypatch.setattr(autonudge_stop_log, "redact_via_context", _policy)
    await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "gave up: corp-key-0042 expired")

    kept = service.get_by_slot("chat-1")
    assert kept is not None
    assert "corp-key-0042" not in kept.stopped_detail
    assert "[REDACTED]" in kept.stopped_detail


@pytest.mark.asyncio
async def test_a_platform_credential_crossing_the_length_bound_is_redacted(
    service, monkeypatch
) -> None:
    """The policy reads the agent's full words before the clip: a credential the
    bound would cut leaves no fragment, since the policy matches it whole first."""
    from kiro_crew import autonudge_stop_log
    from kiro_crew.monitoring.models import MAX_MONITOR_STOP_REASON_CHARS

    def _policy(text: str) -> str:
        return text.replace("corp-key-0042", "[REDACTED]")

    monkeypatch.setattr(autonudge_stop_log, "redact_via_context", _policy)
    await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    padding = "x" * (MAX_MONITOR_STOP_REASON_CHARS - 8)
    await _agent_stops(service, "autonudge_stop", f"{padding}corp-key-0042")

    kept = service.get_by_slot("chat-1")
    assert kept is not None
    assert "corp" not in kept.stopped_detail


@pytest.mark.asyncio
async def test_the_stop_log_reads_the_agents_reason_from_the_row(service) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    before = {loop.id: {"id": loop.id, "slot_key": "chat-1"}}
    await _agent_stops(service, "autonudge_stop", "done")

    rows = json.loads(service._store.path.read_text())["loops"]
    records = stop_records(before, rows, {}, now=0.0)

    assert [(r["reason"], r["detail"]) for r in records] == [(AGENT_STOP_REASON, "done")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "loaded"),
    [
        ("x" * (MAX_MONITOR_STOP_REASON_CHARS + 50), "x" * MAX_MONITOR_STOP_REASON_CHARS),
        (["not", "a", "string"], ""),
        (None, ""),
    ],
    ids=["oversized", "non-string", "null"],
)
async def test_a_stored_detail_loads_as_a_bounded_string(service, stored, loaded) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    service.stop()
    raw = json.loads(service._store.path.read_text())
    raw["loops"][0]["stopped_detail"] = stored
    service._store.path.write_text(json.dumps(raw))

    reloaded = AutoNudgeService(base_dir=service._store.path.parent)
    reloaded._load()
    assert reloaded._loops[loop.id].stopped_detail == loaded


@pytest.mark.asyncio
async def test_stopping_an_already_paused_row_frees_the_slot_for_a_new_arm(service) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await service.update(loop.id, active=False)  # a person's pause: "manual"

    result = await _agent_stops(service, "autonudge_stop", "watching a different PR")

    assert "stopped" in result and "NOT" not in result
    assert service.get_by_slot("chat-1") is None, "a stop that could record nothing removes"
    fresh = await service.add(
        slot_key="chat-1",
        message="watch the other one",
        idle_secs=300,
        replace_existing=False,
        replace_stopped=True,
    )
    assert fresh.active is True


@pytest.mark.asyncio
async def test_a_second_agent_stop_keeps_the_first_stops_record(service) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "PR merged")

    result = await _agent_stops(service, "monitor_stop", "stopping again")

    assert "stopped" in result and "NOT" not in result
    kept = service.get_by_slot("chat-1")
    assert kept is not None and kept.id == loop.id, "a repeat stop does not delete the row"
    assert kept.active is False
    assert kept.stopped_reason == AGENT_STOP_REASON
    assert kept.stopped_detail == "PR merged"


@pytest.mark.asyncio
async def test_monitor_update_after_an_agent_stop_says_the_agent_stopped_it(service) -> None:
    await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    await _agent_stops(service, "autonudge_stop", "done")

    with (
        patch("kiro_crew.autonudge.get_instance", return_value=service),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        patch("kiro_crew.dashboard.session_directive_apply._audit", MagicMock()),
    ):
        result = await apply_session_directive(
            SimpleNamespace(),
            SimpleNamespace(key="chat-1", _app=""),
            "dashboard:chat-1",
            "monitor_update",
            {"patch": {"message": "revised"}},
        )

    assert "you stopped it" in result
    assert "paused manually" not in result


@pytest.mark.asyncio
async def test_a_credential_written_into_the_stored_detail_is_scrubbed_on_load(service) -> None:
    loop = await service.add(slot_key="chat-1", message="watch it", idle_secs=300)
    service.stop()
    raw = json.loads(service._store.path.read_text())
    raw["loops"][0]["stopped_detail"] = "leaked AKIAIOSFODNN7EXAMPLE here"
    service._store.path.write_text(json.dumps(raw))

    reloaded = AutoNudgeService(base_dir=service._store.path.parent)
    reloaded._load()

    assert "AKIAIOSFODNN7EXAMPLE" not in reloaded._loops[loop.id].stopped_detail
    assert reloaded._store_dirty is True, "the scrubbed value is written back"
