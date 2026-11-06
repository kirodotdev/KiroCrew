"""A re-attributed producer's turn takes the person-scoped paths.

Five producers' turns run as their own actor: a queued or idle workflow completion
(``gateway``), a queued or idle heartbeat delivery (``cron``), a queued Issue Radar
crew wake (``crew``), a queued peer ``session_send`` (``other``) and a requeued
work-ledger wake (``autonudge``). Each of these turns takes the refusal-fallback
retry and the per-turn ``model.route`` decision that a ``user`` turn takes, because
the drain hands it ``_person_paths_actor="user"``. These tests run the real drain
and the real runner on both paths. The idle workflow and heartbeat turns pass the
same keywords through their ``functools.partial``, which the composition contract
tests pin.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from chat_runner_coverage_helpers import (
    _coding_tool_call,
    _complete,
    _permission,
    _quiet_sel,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
    _state,
    _statusless_coding_tool_call,
)
from test_dashboard_chat_refusal import TestRunChatRefusalFallback as _RefusalSuite
from test_decisions_model_route_auto_default import (  # noqa: F401 -- fixtures
    _armed_slot,
    _run,
    _switched_to,
    answers,
    clean_registry,
    keystone,
)
from test_queue_producer_tags import _busy, _drain, _idle, _queue_by

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import PERSON_PATHS_ACTOR_META_KEY, TURN_ACTOR_META_KEY
from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND
from kiro_crew.dashboard.chat_utils_injected_rows import AUTOMATION_STEER_META
from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue

#: The queued producers and the actor each one's turn runs as.
QUEUED_PRODUCERS = [
    ("workflow", "gateway"),
    ("heartbeat", "cron"),
    ("crew", "crew"),
    ("peer", "other"),
    ("wake", "autonudge"),
]

#: The idle producers whose turn sets its actor, with the keywords of its partial.
IDLE_PRODUCERS = [
    ("workflow", {"_turn_actor": "gateway", "_person_paths_actor": "user"}),
    ("heartbeat", {"_turn_actor": "cron", "_person_paths_actor": "user"}),
]


def _queue(producer: str, state, slot) -> None:
    if producer == "wake":
        slot.queue_append(
            "wake: 2 reports", meta={AUTOMATION_STEER_META: {"cycle": 3, "loop_id": "L1"}}
        )
    else:
        _queue_by(producer, state, slot)


async def _drained_keywords(tmp_path, producer: str) -> dict:
    """The actor keywords the real drain hands the turn of *producer*'s entry."""
    state = _state(tmp_path / "drain")
    slot = state.get_or_create_slot(f"drain-{producer}")
    slot._titled = True
    _busy(slot)
    _queue(producer, state, slot)
    _idle(slot)

    _started, seen = await _drain(state, slot)

    return {k: seen[0][k] for k in ("_turn_actor", "_person_paths_actor") if k in seen[0]}


async def _refused_turn(tmp_path, monkeypatch, **keywords):
    """Run a refused turn through the real runner with a fallback configured.

    The post-turn drain is held, which keeps the retry queued for the assertions
    instead of running it as a second turn in the background.
    """

    async def _no_drain(_state, _slot, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._configured_refusal_fallback", lambda: "opus-test"
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._start_next_queued_turn", _no_drain)
    state = _RefusalSuite._make_state_for_run_chat(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = _RefusalSuite._make_refusing_client(_RefusalSuite._refusal_events(category="CYBER"))
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    # Read off the runner at call time: the drain helper patches this name, and the
    # ``chat`` facade binds whatever it holds when that module is first imported.
    await chat_runner._run_chat(state, slot, "machine-produced prompt", **keywords)
    return slot, client


@pytest.mark.asyncio
@pytest.mark.parametrize(("producer", "actor"), QUEUED_PRODUCERS)
async def test_the_drain_keeps_the_person_paths_of_a_queued_producer(tmp_path, producer, actor):
    keywords = await _drained_keywords(tmp_path, producer)

    assert keywords == {"_turn_actor": actor, "_person_paths_actor": "user"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("producer", "actor"), QUEUED_PRODUCERS)
async def test_a_queued_producers_turn_carries_no_user_origin(tmp_path, producer, actor):
    """The milestone count (``record_user_turn``) needs the user-origin mark as
    well as the ``user`` actor. These turns carry no user-origin mark, which keeps
    them out of the count under any actor."""
    state = _state(tmp_path)
    slot = state.get_or_create_slot(f"origin-{producer}")
    slot._titled = True
    _busy(slot)
    _queue(producer, state, slot)
    _idle(slot)

    _started, seen = await _drain(state, slot)

    assert seen[0]["_directive_user_origin"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["person", "cron-notification"])
async def test_the_drain_names_no_person_paths_actor_for_other_entries(tmp_path, producer):
    state = _state(tmp_path)
    slot = state.get_or_create_slot(f"other-{producer}")
    slot._titled = True
    _busy(slot)
    if producer == "cron-notification":
        slot.queue_append('[Cron notification from "scan"]\nok', kind=CRON_NOTIFICATION_KIND)
    else:
        _queue_by(producer, state, slot)
    _idle(slot)

    _started, seen = await _drain(state, slot)

    assert "_person_paths_actor" not in seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("producer", "actor"), QUEUED_PRODUCERS)
async def test_a_queued_producers_refused_turn_still_retries_on_the_fallback(
    tmp_path, monkeypatch, producer, actor
):
    keywords = await _drained_keywords(tmp_path, producer)

    slot, client = await _refused_turn(tmp_path, monkeypatch, **keywords)

    client.set_model.assert_awaited_once_with("opus-test")
    assert slot._refusal_fallback_attempted is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("producer", "keywords"), IDLE_PRODUCERS)
async def test_an_idle_producers_refused_turn_still_retries_on_the_fallback(
    tmp_path, monkeypatch, producer, keywords
):
    slot, client = await _refused_turn(tmp_path, monkeypatch, **keywords)

    client.set_model.assert_awaited_once_with("opus-test")
    assert slot._refusal_fallback_attempted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["crew", "cron", "subagent"])
async def test_a_turn_without_the_person_paths_keeps_the_terminal_refusal(
    tmp_path, monkeypatch, actor
):
    """An idle crew wake runs as ``crew`` with no person-paths actor, and a cron or
    sub-agent turn is unattended. The retry skips all three."""
    slot, client = await _refused_turn(tmp_path, monkeypatch, _turn_actor=actor)

    client.set_model.assert_not_awaited()
    assert slot._refusal_fallback_attempted is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("producer", "actor"), QUEUED_PRODUCERS)
async def test_a_queued_producers_turn_still_routes_on_an_armed_slot(
    tmp_path, keystone, answers, producer, actor  # noqa: F811 -- fixtures
):
    keystone(True)
    answers("complex")
    keywords = await _drained_keywords(tmp_path, producer)

    client = await _run(
        tmp_path, _armed_slot(f"route-{producer}"), _directive_user_origin=False, **keywords
    )

    assert _switched_to(client) == ["model-c"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("producer", "keywords"), IDLE_PRODUCERS)
async def test_an_idle_producers_turn_still_routes_on_an_armed_slot(
    tmp_path, keystone, answers, producer, keywords  # noqa: F811 -- fixtures
):
    keystone(True)
    answers("complex")

    client = await _run(
        tmp_path, _armed_slot(f"route-idle-{producer}"), _directive_user_origin=False, **keywords
    )

    assert _switched_to(client) == ["model-c"]


@pytest.mark.asyncio
async def test_a_recovery_carries_the_person_paths_actor_to_its_retry(tmp_path, monkeypatch):
    """The refusal retry is itself a queued recovery, and its turn reads the
    person-paths actor of the refused turn."""
    slot, _client = await _refused_turn(
        tmp_path, monkeypatch, _turn_actor="gateway", _person_paths_actor="user"
    )

    recoveries = [q for q in slot._queue if q.get("content") == "machine-produced prompt"]
    assert recoveries, f"the refusal retry queued no recovery entry, queue={slot._queue}"
    assert recoveries[0]["meta"][TURN_ACTOR_META_KEY] == "gateway"
    assert recoveries[0]["meta"][PERSON_PATHS_ACTOR_META_KEY] == "user"


def test_the_restore_strips_the_person_paths_actor():
    restored = sanitize_restored_queue(
        [{"id": "q1", "content": "edited", "meta": {PERSON_PATHS_ACTOR_META_KEY: "user"}}]
    )

    assert PERSON_PATHS_ACTOR_META_KEY not in restored[0]["meta"]


def test_the_drain_reads_a_recoverys_person_paths_actor():
    from kiro_crew.dashboard.chat_turn.steer_queue import _person_paths_actor_for_queue_items

    stamped = {
        "content": "x",
        "meta": {TURN_ACTOR_META_KEY: "cron", PERSON_PATHS_ACTOR_META_KEY: "user"},
    }
    unstamped = {"content": "x", "meta": {TURN_ACTOR_META_KEY: "cron"}}

    assert _person_paths_actor_for_queue_items([stamped]) == "user"
    assert _person_paths_actor_for_queue_items([unstamped]) == ""


#: What a coding turn's WakaTime heartbeat carries, less its timestamp.
_WAKATIME_BEAT = {
    "entity": "kirocrew-session",
    "type": "app",
    "category": "ai coding",
    "is_write": True,
}


@pytest.fixture
def sent_beats(monkeypatch):
    """Every WakaTime heartbeat payload the turn hands to the sender, in order."""
    from kiro_crew.wakatime import heartbeats

    sent: list[dict] = []
    monkeypatch.setattr(heartbeats, "_schedule_send", sent.append)
    return sent


def _beat_shapes(beats):
    return [{k: v for k, v in beat.items() if k != "time"} for beat in beats]


def _wakatime_config():
    config = chat_runner.KiroCrewConfig.load()
    config.wakatime.enabled = True
    config.wakatime.send_heartbeats = True
    return config


async def _coding_turn(tmp_path, *, cadence: bool, **keywords):
    """Run a coding turn through the real runner with WakaTime heartbeats on.

    With *cadence* the coding call waits for an approval, which emits the
    cadence beat, and the end-of-turn beat is stubbed out. Without it the call
    runs with no permission prompt, and only the end-of-turn beat is live.
    """
    state, client = _runner_state(tmp_path)
    slot = _slot(f"chat-wt-{'cadence' if cadence else 'end'}")
    if cadence:
        _set_stream(
            client,
            [
                _coding_tool_call("tc-wt"),
                _permission(tool_kind="edit", tool_call_id="tc-wt", is_shell=False),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        stub = patch.object(chat_runner, "note_coding_activity")
    else:
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])
        stub = patch.object(chat_runner, "note_coding_cadence")
    # The empty-response retries start spent, so a turn with no assistant text
    # does not requeue a second turn.
    slot._empty_response_retries = 2
    with (
        patch.object(chat_runner.KiroCrewConfig, "load", return_value=_wakatime_config()),
        stub,
        _quiet_sel(),
    ):
        await chat_runner._run_chat(state, slot, "write the file", **keywords)
    await _settle(slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("cadence", [False, True], ids=["end-of-turn", "cadence"])
@pytest.mark.parametrize(("producer", "actor"), QUEUED_PRODUCERS)
async def test_a_queued_producers_coding_turn_sends_the_wakatime_heartbeat(
    tmp_path, sent_beats, producer, actor, cadence
):
    keywords = await _drained_keywords(tmp_path, producer)

    await _coding_turn(tmp_path, cadence=cadence, **keywords)

    assert _beat_shapes(sent_beats) == [_WAKATIME_BEAT]


@pytest.mark.asyncio
@pytest.mark.parametrize("cadence", [False, True], ids=["end-of-turn", "cadence"])
@pytest.mark.parametrize(("producer", "keywords"), IDLE_PRODUCERS)
async def test_an_idle_producers_coding_turn_sends_the_wakatime_heartbeat(
    tmp_path, sent_beats, producer, keywords, cadence
):
    await _coding_turn(tmp_path, cadence=cadence, **keywords)

    assert _beat_shapes(sent_beats) == [_WAKATIME_BEAT]


@pytest.mark.asyncio
@pytest.mark.parametrize("cadence", [False, True], ids=["end-of-turn", "cadence"])
@pytest.mark.parametrize("actor", ["crew", "cron", "subagent"])
async def test_a_coding_turn_without_the_person_paths_sends_no_wakatime_heartbeat(
    tmp_path, sent_beats, actor, cadence
):
    await _coding_turn(tmp_path, cadence=cadence, _turn_actor=actor)

    assert sent_beats == []
