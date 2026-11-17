"""Restart resume, continued: the notice dedupe and status order, presses that fail,
the Trust note at redraw time, and the invariant that a turn's identity comes only
from what the dispatcher produced. Split from ``test_slack_code_channel_resume``
(the file line cap); its ``world`` and ``stops`` fixtures and helpers are shared.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_slack_code_channel_dispatch_exemption import CHANNEL, OWNER
from test_slack_code_channel_resume import (  # noqa: F401 - world and stops are fixtures
    _assert_not_done,
    _auto_notice,
    _buttons,
    _click,
    _dispatched,
    _held_notice,
    _interrupt,
    _own_worker,
    _restart,
    _running,
    _started,
    _texts,
    stops,
    world,
)

from kiro_crew.slack.code_channels import resume, status

# ── green round: the dedupe, the status order, the given-back button ───────


def test_a_newly_resumed_worker_gets_a_notice_while_the_conductor_stays_held(
    world, stops  # noqa: F811
):
    """The held conductor's button already exists, but a later restart resumed a
    worker: a fresh notice is posted and its Stop reaches that worker."""
    _held_notice(world)
    posted = len(world.slack.posts)
    worker = _own_worker(world)
    _interrupt(worker)
    (report,) = _restart(world)
    assert report.held == [world.slot.key] and report.resumed == [worker.key]
    assert len(world.slack.posts) == posted + 1, "the resumed worker needs its own notice"
    record = world.store.resume_record(f"notice:{CHANNEL}")
    assert [k for k, _ in resume._resumed_turns(record)] == [worker.key]
    (stop,) = [
        b
        for b in _buttons(world.slack.posts[-1][1])
        if b["action_id"].startswith(resume.STOP_ACTION_PREFIX)
    ]
    _running(worker)
    ts = f"1700000000.00000{len(world.slack.posts)}"
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, stop)))
    assert [k for k, _ in stops] == [worker.key]


def test_a_held_set_with_nothing_new_resumed_posts_no_second_notice(world):  # noqa: F811
    _held_notice(world)
    posted = len(world.slack.posts)
    _restart(world)
    assert len(world.slack.posts) == posted


def test_the_stale_status_is_settled_before_the_conductor_resumes(world, monkeypatch):  # noqa: F811
    """The resumed turn sets ``processing`` as it starts; a settle after it would
    leave the channel showing ``active`` while the turn runs."""
    order: list[str] = []

    async def _settle(channel: str) -> None:
        order.append("settle")

    async def _go(state_: Any, target: Any, still_ok: Any = None, **_kw: Any) -> str:
        order.append(f"continue:{target.key}")
        return ""

    monkeypatch.setattr(status, "settle_after_stop", _settle)
    monkeypatch.setattr(resume, "_continue", _go)
    _interrupt(world.slot)
    _restart(world)
    assert order == ["settle", f"continue:{world.slot.key}"]


def test_a_conductor_turn_already_running_keeps_its_status(world, monkeypatch):  # noqa: F811
    """A message reached the conductor before the pass did: its live turn owns
    the status, so no settle overwrites its ``processing``."""
    settles: list[str] = []

    async def _settle(channel: str) -> None:
        settles.append(channel)

    monkeypatch.setattr(status, "settle_after_stop", _settle)
    _interrupt(world.slot)
    _running(world.slot)
    _restart(world)
    assert settles == []


def test_only_a_workers_interruption_settles_nothing(world, monkeypatch):  # noqa: F811
    """Workers are not bound to the channel; its status is the conductor's."""
    settles: list[str] = []

    async def _settle(channel: str) -> None:
        settles.append(channel)

    monkeypatch.setattr(status, "settle_after_stop", _settle)
    worker = _own_worker(world)
    _interrupt(worker)
    (report,) = _restart(world)
    assert report.resumed == [worker.key]
    assert settles == []


def test_a_given_back_button_names_only_what_is_still_held(world, monkeypatch):  # noqa: F811
    worker = world.state.get_or_create_slot("chat-held-worker")
    worker._created_by = world.slot.key
    worker.title = "Retry backoff tests"
    world.store.record_worker(worker.key, world.slot.key)
    ts, button = _held_notice(world)
    _interrupt(worker)

    async def _one_refuses(state, slot, still_ok=None, **_kw):
        return "slot_queue_pending" if slot is worker else _started(slot)

    monkeypatch.setattr(resume, "_continue", _one_refuses)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    record = world.store.resume_record(f"notice:{CHANNEL}")
    assert record["state"] == "button"
    assert record["resume_label"] == "Resume Retry backoff tests"
    redrawn = world.slack.updates[-1][3]
    labels = [
        b["text"]["text"]
        for b in _buttons(redrawn)
        if b["action_id"].startswith(resume.RESUME_ACTION_PREFIX)
    ]
    assert labels == ["Resume Retry backoff tests"]


def test_a_store_failure_while_updating_the_notice_reads_as_lost(world, monkeypatch):  # noqa: F811
    ts, _button = _held_notice(world)

    def _boom(*_a: Any, **_k: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(world.store, "replace_resume_if", _boom)
    out = asyncio.run(
        resume._update_notice(f"notice:{CHANNEL}", ts, lambda c: True, lambda c: dict(c))
    )
    assert out is None


# ── a press that fails or is cancelled, and the Trust note at redraw time ──


def test_a_resume_press_that_raises_gives_the_button_back(world, monkeypatch):  # noqa: F811
    ts, button = _held_notice(world)
    monkeypatch.setattr(resume, "_resume_on_press", AsyncMock(side_effect=OSError("disk")))
    assert asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button))) is True
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"
    assert world.slack.ephemerals[-1][2].startswith("Nothing could be resumed right now")
    _assert_not_done(world)


def test_a_cancelled_resume_press_gives_the_button_back(world, monkeypatch):  # noqa: F811
    ts, button = _held_notice(world)
    monkeypatch.setattr(resume, "_resume_on_press", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


def test_a_trust_scope_reads_as_trust_on_the_notice(world):  # noqa: F811
    """The note follows the mode the header shows: a live trust scope is Trust."""
    world.slot._trust_scope = "channel"
    _interrupt(world.slot)
    _restart(world)
    assert _texts(world)[0].endswith(resume.TRUST_NOTE)


def test_the_trust_note_follows_a_mode_change_on_redraw(world):  # noqa: F811
    world.slot._trust = True
    ts, button = _held_notice(world)
    assert world.slack.posts[-1][1][0]["text"]["text"].endswith(resume.TRUST_NOTE)
    assert resume.TRUST_NOTE not in world.store.resume_record(f"notice:{CHANNEL}")["text"]
    world.slot._trust = False  # the owner lowered the channel to Ask from Slack
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    redrawn = world.slack.updates[-1]
    assert resume.TRUST_NOTE not in redrawn[2]
    assert resume.TRUST_NOTE not in redrawn[3][0]["text"]["text"]
    world.slot._trust = True  # raised again: the next redraw says so
    asyncio.run(resume._redraw(CHANNEL, ts, f"notice:{CHANNEL}"))
    assert world.slack.updates[-1][3][0]["text"]["text"].endswith(resume.TRUST_NOTE)


def test_an_older_records_trust_note_is_not_doubled_on_redraw(world):  # noqa: F811
    world.slot._trust = True
    ts, _button = _held_notice(world)
    key = f"notice:{CHANNEL}"
    record = world.store.resume_record(key)
    # A record whose text already carries the note, as an earlier build stored it.
    world.store.record_resume(key, {**record, "text": f"{record['text']} {resume.TRUST_NOTE}"})
    asyncio.run(resume._redraw(CHANNEL, ts, key))
    text = world.slack.updates[-1][3][0]["text"]["text"]
    assert text.endswith(resume.TRUST_NOTE) and text.count(resume.TRUST_NOTE) == 1


# ── the invariant: a turn's identity is the dispatcher's, never a row's look ──
#
# These spell the recovery row's stamp as its literal key, so they read the same
# against any version of the module.

_STEER = {
    "role": "user",
    "content": "also check the docs",
    "ts": "2026-10-19T10:06:00+00:00",
    "meta": {"steer": True},
}
#: A channel message queued behind the running turn: its row is written on arrival.
_QUEUED = {"role": "user", "content": "and then deploy", "ts": "2026-10-19T10:06:30+00:00"}


def _resumed_then_cut(w, *written: dict) -> None:
    """Restart 1 resumes T; restart 2 cuts that resumed run, with *written* rows
    (nothing dispatched) after its recovery row."""
    _interrupt(w.slot)
    _restart(w)
    (recovery,) = [row for row in w.slot.messages if row.get("role") == "inject"]
    w.slot.messages.extend(dict(row) for row in written)
    w.slot.messages.append(
        {"role": "error", "content": "connection lost", "ts": "2026-10-19T10:07:00+00:00"}
    )
    w.slot._restored_turn_marker = {
        "generation": 1,
        "ts": recovery["ts"],
        "mid": recovery["meta"]["mid"],
    }


@pytest.mark.parametrize(
    "written", [[_STEER], [_QUEUED], [_STEER, _QUEUED]], ids=["steer", "queued", "both"]
)
def test_at_most_one_automatic_resume_with_rows_that_open_nothing(world, written):  # noqa: F811
    """A steer and a queued channel message LOOK like openers. Neither was
    dispatched, so a second restart still reads the resumed run as T: held."""
    _resumed_then_cut(world, *written)
    (report,) = _restart(world)
    assert world.continued == [world.slot.key], "never resumed twice"
    assert report.held == [world.slot.key]
    assert report.why[world.slot.key] == resume.HELD_ALREADY


@pytest.mark.parametrize(
    "written", [[_STEER], [_QUEUED], [_STEER, _QUEUED]], ids=["steer", "queued", "both"]
)
def test_stop_stops_the_resumed_run_past_rows_that_open_nothing(
    world, stops, written  # noqa: F811
):
    ts, button = _auto_notice(world)  # noqa: F811
    world.slot.messages.extend(dict(row) for row in written)
    _running(world.slot)
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert [k for k, _ in stops] == [world.slot.key]


#: Every producer that appends a row to a code channel session's transcript, and
#: whether it STARTS a turn. Starting one is a dispatch: the slot's generation
#: moves and the turn marker names the dispatcher's row. A producer that only
#: writes a row opens nothing, whatever the row looks like. A turn-opening role
#: or inject kind missing here fails ``test_every_turn_opening_kind_is_classified``.
PRODUCERS: list[tuple[str, str | None, str | None, bool, bool]] = [
    # (producer, row role, injectKind, steer, starts a turn)
    ("queue drain: a person's message", "user", None, False, True),
    ("queue drain: sub-agent completion", "subagent", None, False, True),
    ("queue drain: monitor wake", "nudge", None, False, True),
    ("queue drain: cron run", "inject", "cron", False, True),
    ("queue drain: MCP app message", "inject", "mcp_app", False, True),
    ("queue drain: automation notice", "inject", "automation_notice", False, True),
    ("queue drain: heartbeat prompt", "inject", "heartbeat", False, True),
    ("queue drain: workflow follow-up", "inject", "workflow", False, True),
    ("queue drain: Resume or restart resume", "inject", "recovery", False, True),
    ("queue drain: replay of the person's words", "inject", "user_replay", False, True),
    ("queue cycle: sub-agent synthesis", "inject", "synthesis", False, True),
    ("agent-started turn (writes no opening row)", None, None, False, True),
    ("Slack message behind a running turn (written on arrival)", "user", None, False, False),
    ("steer into a running turn", "user", None, True, False),
    ("monitor steer into a running turn", "nudge", None, True, False),
    ("chat runner reply", "assistant", None, False, False),
    ("chat runner tool row", "tool", None, False, False),
    ("chat runner error, restart interruption row", "error", None, False, False),
    ("/note, refusal or stop-hook card (untagged inject)", "inject", None, False, False),
]


def test_every_turn_opening_kind_is_classified():
    from kiro_crew.dashboard.chat_handlers import _TURN_OPENER_ROLES
    from kiro_crew.dashboard.state_parts.transcript_rows import _TURN_INJECT_KINDS

    starting = {(role, kind) for _, role, kind, _steer, starts in PRODUCERS if starts}
    assert {(role, None) for role in _TURN_OPENER_ROLES} <= starting
    assert {("inject", kind) for kind in _TURN_INJECT_KINDS} <= starting


@pytest.mark.parametrize(
    ("producer", "role", "kind", "steer", "starts"), PRODUCERS, ids=[p[0] for p in PRODUCERS]
)
def test_only_a_dispatch_moves_the_turn(
    world, stops, producer, role, kind, steer, starts  # noqa: F811
):
    """During the resumed run, each producer acts as it does in the gateway. The
    notice's Stop stops that run unless the producer dispatched a newer turn."""
    ts, button = _auto_notice(world)
    row = None
    if role is not None:
        meta = {"injectKind": kind} if kind else {}
        if steer:
            meta["steer"] = True
        row = {"role": role, "content": "x", "ts": "2026-10-19T11:00:00+00:00", "meta": meta}
        world.slot.messages.append(row)
    if starts:
        world.slot._turn_generation += 1
        world.slot._turn_in_flight_prompt = row
    _running(world.slot)
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert (stops == []) is starts, producer


def test_stop_never_reaches_a_same_key_slot_reopened_since(world, stops):  # noqa: F811
    """A worker closed and reopened under its key counts dispatches from 0 again,
    so its first turn reaches the resumed run's generation: the notice's Stop
    still leaves that new session's turn alone."""
    worker = _own_worker(world, "chat-reopened-worker")
    ts, _ = _auto_notice(world, worker)
    del world.state._slots[worker.key]
    reopened = _own_worker(world, worker.key)
    assert reopened is not worker
    _dispatched(reopened, "user", "2026-10-19T12:00:00+00:00")
    _running(reopened)
    stop = next(
        b
        for b in _buttons(world.slack.posts[-1][1])
        if b["action_id"].startswith(resume.STOP_ACTION_PREFIX)
    )
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, stop)))
    assert reopened.key not in [k for k, _ in stops]


def test_a_resume_the_drain_holds_is_not_reported_as_running(world, monkeypatch):  # noqa: F811
    """Queued, but the drain started nothing (a Pause all, the gateway going
    away): no "I resumed where I left off", and nothing for Stop to name."""

    async def _queued_only(state_: Any, target: Any, still_ok: Any = None, **_kw: Any) -> str:
        return ""  # queued; no dispatch, so the generation does not move

    monkeypatch.setattr(resume, "_continue", _queued_only)
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.resumed == [] and report.held == [world.slot.key]
    assert resume.AUTO_TEXT not in _texts(world)


def test_the_under_lock_recheck_refuses_while_the_crew_is_paused(world, monkeypatch):  # noqa: F811
    from kiro_crew import crew_pause

    _interrupt(world.slot)
    seen = resume._dispatch_token(world.slot)
    check = resume._recheck_for(world.state, CHANNEL, world.slot, world.slot, seen)
    assert check() == ""
    monkeypatch.setattr(crew_pause, "holds_slot", lambda slot: slot is world.slot)
    assert check() == "its crew is paused"


def test_stop_leaves_a_later_send_that_is_still_being_admitted(world, stops):  # noqa: F811
    """The resumed run finished; a later send has reserved the session but not yet
    been dispatched. The generation still reads as the resumed run's, so Stop asks
    whether that run's own task is live, not whether the session is reserved."""
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True  # reserved, no task
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert stops == []


def test_stop_reaches_the_resumed_runs_own_retry(world, stops):  # noqa: F811
    """The resumed run retries itself (a transient error, a reconnect): the drain
    dispatches its own recovery under a new generation, but it is still the
    turn the notice resumed, so Stop stops it."""
    ts, button = _auto_notice(world)
    resumed = world.slot._turn_generation
    world.slot._turn_generation += 1  # the retry's dispatch
    world.slot._turn_lineage = (world.slot._turn_generation, resumed)
    _running(world.slot)
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert [k for k, _ in stops] == [world.slot.key]
