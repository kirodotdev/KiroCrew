"""Resuming Slack code channels a gateway restart interrupted.

The pass continues an interrupted channel session and ITS OWN interrupted
workers once per interrupted turn, posts one notice, and falls back to an
owner-only Resume button when a turn was already resumed once (its resumed run
was interrupted again, or the gateway went down during it).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_session_control_owner_dm import _state
from test_slack_code_channel_dispatch_exemption import ANCHOR, CHANNEL, OWNER, TEAM, _channel

from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.code_channels import resume, routing, status
from kiro_crew.slack.code_channels import surface as cc_surface
from kiro_crew.slack.code_channels.store import Binding, set_store

OTHER = "U0OTHER0001"
_REAL_CONTINUE = resume._continue


class _Slack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, list[dict], str]] = []
        self.updates: list[tuple[str, str, str, list[dict] | None]] = []
        self.ephemerals: list[tuple[str, str, str]] = []

    async def post_blocks(self, channel: str, blocks: list[dict], text: str, thread_ts=None) -> str:
        self.posts.append((channel, blocks, text))
        return f"1700000000.00000{len(self.posts)}"

    async def update_message(self, channel: str, ts: str, text: str = "", blocks=None) -> None:
        self.updates.append((channel, ts, text, blocks))

    async def post_ephemeral(self, channel: str, user: str, text: str) -> None:
        self.ephemerals.append((channel, user, text))


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, store = _channel(state, tmp_path)
    slack = _Slack()
    audits: list[tuple] = []
    continued: list[str] = []

    async def _fake_continue(state_: Any, target: Any, still_ok: Any = None, **_kw: Any) -> str:
        if still_ok is not None and still_ok():
            return "code_channel_resume_changed"
        continued.append(target.key)
        return ""

    monkeypatch.setattr(resume, "_continue", _fake_continue)
    monkeypatch.setattr(resume, "code_channels_on", lambda cfg: True)
    monkeypatch.setattr(resume, "live_config", lambda orch=None: object())
    monkeypatch.setattr(cc_surface, "slack", lambda: slack)
    monkeypatch.setattr(cc_surface, "audit", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(cc_surface, "outbound_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_surface, "is_archived", AsyncMock(return_value=False))
    monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: team == TEAM)
    monkeypatch.setattr(cc_surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(status, "settle_after_stop", AsyncMock())
    monkeypatch.setattr(routing, "_origin_of", lambda channel, binding, x: "C0ORIGIN01")
    monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, channel, origin: True)
    monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    yield SimpleNamespace(
        state=state, slot=slot, store=store, slack=slack, audits=audits, continued=continued
    )
    set_store(None)


def _assert_not_done(world) -> None:
    """A failed press: no success line, and the message still offers a button."""
    for _, _, _, blocks in world.slack.updates:
        text = json.dumps(blocks)
        assert "Resumed by" not in text and "Stopped by" not in text
    if world.slack.updates:
        blocks = world.slack.updates[-1][3]
        assert any(b.get("accessory") or b["type"] == "actions" for b in blocks), "button kept"


def _interrupt(slot: Any, ts: str = "2026-10-19T10:00:00+00:00") -> None:
    """The transcript a restart leaves mid-turn: a request with no reply."""
    slot.messages.append({"role": "user", "content": "fix it", "ts": ts})


def _restart(world) -> list[resume.ResumeReport]:
    return asyncio.run(resume.resume_after_restart(world.state))


def _texts(world) -> list[str]:
    return [blocks[0]["text"]["text"] for _, blocks, _ in world.slack.posts]


def test_an_interrupted_channel_auto_resumes_once(world):
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert world.continued == [world.slot.key]
    assert report.resumed == [world.slot.key] and not report.held
    assert _texts(world) == [resume.AUTO_TEXT]
    (stop,) = world.slack.posts[0][1][-1]["elements"]
    assert stop["action_id"].startswith(resume.STOP_ACTION_PREFIX)
    assert stop["text"]["text"] == f"Stop {world.slot.title or world.slot.key}"
    status.settle_after_stop.assert_awaited_with(CHANNEL)
    assert any(a[1] == resume.OPERATION and a[2] == "allowed" for a in world.audits)
    record = world.store.resume_record(world.slot.key)
    assert record and record["turn"] == "2026-10-19T10:00:00+00:00"


def test_the_attempt_is_recorded_before_the_continue(world, monkeypatch):
    seen: list[Any] = []

    async def _check(state_: Any, target: Any, still_ok: Any = None, **_kw: Any) -> str:
        seen.append(world.store.resume_record(target.key))
        return ""

    monkeypatch.setattr(resume, "_continue", _check)
    _interrupt(world.slot)
    _restart(world)
    assert seen and seen[0] is not None, "the loop guard exists before the turn runs"


def test_a_second_interruption_of_the_same_turn_posts_the_button(world):
    _interrupt(world.slot)
    _restart(world)
    # The resumed run is interrupted too (its recovery row continues the same turn).
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    (report,) = _restart(world)
    assert world.continued == [world.slot.key], "never resumed twice"
    assert report.held == [world.slot.key]
    assert _texts(world) == [resume.AUTO_TEXT, resume.held_text([resume.HELD_ALREADY])]
    button = world.slack.posts[-1][1][-1]["elements"][0]
    assert button["action_id"].startswith(resume.RESUME_ACTION_PREFIX)
    assert button["text"]["text"] == f"Resume {world.slot.title or world.slot.key}"


def test_no_duplicate_notices_after_repeated_restarts(world):
    _interrupt(world.slot)
    for _ in range(4):
        _restart(world)
    assert _texts(world) == [resume.AUTO_TEXT, resume.held_text([resume.HELD_ALREADY])]
    assert world.continued == [world.slot.key]


def test_a_refused_continue_falls_back_to_the_button(world, monkeypatch):
    monkeypatch.setattr(resume, "_continue", AsyncMock(return_value="slot_running"))
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.held == [world.slot.key]
    assert _texts(world) == [resume.held_text([resume.HELD_REFUSED])]


def test_a_finished_turn_is_left_alone(world):
    world.slot.messages.append({"role": "user", "content": "q", "ts": "2026-10-19T09:00:00+00:00"})
    world.slot.messages.append(
        {"role": "assistant", "content": "a", "ts": "2026-10-19T09:00:01+00:00"}
    )
    assert _restart(world) == [resume.ResumeReport(channel_id=CHANNEL)]
    assert world.continued == [] and world.slack.posts == []


def test_only_its_own_workers_resume(world):
    state = world.state
    own = state.get_or_create_slot("chat-own-worker")
    own._created_by = world.slot.key
    world.store.record_worker(own.key, world.slot.key)
    stranger = state.get_or_create_slot("chat-somebody-else")
    stranger._created_by = "chat-another-conductor"
    world.store.record_worker(stranger.key, "chat-another-conductor")
    person = state.get_or_create_slot("chat-the-persons-own-tab")
    for slot in (own, stranger, person):
        _interrupt(slot)
    world.store.record_worker("chat-gone-worker", world.slot.key)
    (report,) = _restart(world)
    assert world.continued == [own.key]
    assert report.resumed == [own.key] and report.lost == ["chat-gone-worker"]


def test_a_held_back_worker_is_reported_lost(world):
    own = world.state.get_or_create_slot("chat-own-worker")
    own._created_by = world.slot.key
    world.store.record_worker(own.key, world.slot.key)
    _interrupt(own)
    _restart(world)
    reports = _restart(world)  # interrupted again: held back
    assert reports[-1].held == [own.key] and own.key in reports[-1].lost


def test_archived_or_wrapped_up_channels_are_skipped(world):
    _interrupt(world.slot)
    binding = world.store.get_binding(CHANNEL)
    world.store.update_binding(CHANNEL, archived_at=1.0)
    assert _restart(world) == []
    world.store.update_binding(CHANNEL, archived_at=0.0, wrapped_up_at=1.0)
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []
    assert binding is not None


def test_a_trust_channel_says_the_work_runs_without_prompts(world):
    world.slot._trust = True
    _interrupt(world.slot)
    _restart(world)
    assert resume.TRUST_NOTE in _texts(world)[0]


# ── the Resume button ──────────────────────────────────────────────────────


def _held_notice(world) -> tuple[str, dict]:
    _interrupt(world.slot)
    _restart(world)
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    _restart(world)
    ts = f"1700000000.00000{len(world.slack.posts)}"
    button = world.slack.posts[-1][1][-1]["elements"][0]
    return ts, button


def _click(user: str, ts: str, button: dict) -> dict[str, Any]:
    return {
        "team": {"id": TEAM},
        "user": {"id": user},
        "channel": {"id": CHANNEL},
        "message": {"ts": ts},
        "actions": [{"action_id": button["action_id"], "value": button["value"]}],
    }


def test_the_resume_button_is_owner_only(world):
    ts, button = _held_notice(world)
    assert asyncio.run(resume.handle_resume_click(None, _click(OTHER, ts, button))) is True
    assert world.slack.ephemerals[-1] == (
        CHANNEL,
        OTHER,
        resume.NOT_OWNER_TEXT.format(owner=cc_surface.owner_name()),
    )
    assert world.continued == [world.slot.key], "nothing more was continued"


def test_the_owners_resume_continues_once(world):
    ts, button = _held_notice(world)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.continued == [world.slot.key, world.slot.key]
    assert world.slack.updates and world.slack.updates[-1][1] == ts
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.continued == [world.slot.key, world.slot.key], "single use"
    assert world.slack.ephemerals[-1] == (CHANNEL, OWNER, resume.STALE_TEXT)
    assert any(a[1] == resume.OPERATION and "via=button" in a[3] for a in world.audits)


def test_a_forged_resume_button_is_refused(world):
    ts, button = _held_notice(world)
    other = dict(button, value=json.dumps({"channel": "C0SOMEWHERE"}))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, other)))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, "1700000099.000001", button)))
    assert world.continued == [world.slot.key]
    assert world.slack.ephemerals[-2:] == [
        (CHANNEL, OWNER, resume.NOT_ALLOWED_TEXT),
        (CHANNEL, OWNER, resume.STALE_TEXT),
    ]


def test_the_router_dispatches_the_resume_prefix(world, monkeypatch):
    from kiro_crew.slack import interactions

    monkeypatch.setattr(interactions, "is_allowed_user", lambda user: True)
    monkeypatch.setattr(interactions, "channel_inbound_permitted", AsyncMock(return_value=True))
    ts, button = _held_notice(world)
    asyncio.run(interactions.dispatch({"type": "block_actions", **_click(OWNER, ts, button)}))
    assert world.continued == [world.slot.key, world.slot.key]


def test_the_real_continue_uses_the_resume_path(world, monkeypatch):
    """``_continue`` is the Resume button's own path, refusing a finished turn."""
    import kiro_crew.dashboard.chat_handlers as chat_handlers

    calls: list[dict[str, Any]] = []

    async def _fake(state_: Any, slot: Any, **kw: Any) -> str:
        calls.append(kw)
        return "resumed"

    monkeypatch.setattr(resume, "_continue", _REAL_CONTINUE)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _fake)
    assert asyncio.run(resume._continue(world.state, world.slot)) == ""
    assert calls[-1]["require_interrupted"] is True
    assert calls[-1]["directive_user_origin"] is False
    assert calls[-1]["dispatch_guard"] is resume._session_map_untaken


def test_resume_waits_for_slack_then_runs(world, monkeypatch):
    ran: list[Any] = []
    monkeypatch.setattr(
        resume, "resume_after_restart", AsyncMock(side_effect=lambda s: ran.append(s))
    )
    flips = iter([False, True])
    asyncio.run(resume.resume_when_ready(world.state, lambda: next(flips, True), sleep=AsyncMock()))
    assert ran == [world.state]


def test_binding_is_unchanged_by_a_resume(world):
    before = world.store.get_binding(CHANNEL)
    _interrupt(world.slot)
    _restart(world)
    assert world.store.get_binding(CHANNEL) == before
    assert before is not None and before.session_key == world.slot.key
    assert before.anchor_ts == ANCHOR and isinstance(before, Binding)


def test_a_malformed_resume_table_reads_as_unavailable(tmp_path):
    """The loop guard lives in the write-protected store; a damaged table must not
    read as "never resumed" (which would let a crashing turn loop)."""
    from kiro_crew.slack.code_channels.store import CodeChannelStore

    path = tmp_path / "slack-code-channels.json"
    path.write_text(json.dumps({"resume": {"chat-x": {"turn": 3}}}), encoding="utf-8")
    loaded = CodeChannelStore(path=path)
    loaded.load()
    assert loaded.unreadable is True
    path.write_text(json.dumps({"resume": {"chat-x": {"turn": "t"}}}), encoding="utf-8")
    good = CodeChannelStore(path=path)
    good.load()
    assert good.resume_record("chat-x") == {"turn": "t"}


# ── round-15 review: identity, admission, stale buttons, races ─────────────


def test_an_unknown_turn_identity_is_held_not_resumed(world):
    """The opener pushed out of the window: missing identity is never a new turn."""
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    world.slot.messages.append(
        {"role": "error", "content": "boom", "ts": "2026-10-19T10:06:00+00:00"}
    )
    (report,) = _restart(world)
    assert world.continued == [] and report.held == [world.slot.key]
    assert _texts(world) == [resume.held_text([resume.HELD_UNKNOWN])]


def test_a_tab_handed_the_anchor_is_not_resumed(world):
    other = world.state.get_or_create_slot("chat-the-persons-own-tab")
    _interrupt(other)
    world.state.link_slack(other.key, ANCHOR, CHANNEL)  # the anchor now maps to B
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []


def test_a_worker_relinked_elsewhere_is_not_resumed(world):
    worker = world.state.get_or_create_slot("chat-own-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    _interrupt(worker)
    worker.linked_session_key = "slack:C0ELSEWHERE:1.0"
    _restart(world)
    assert world.continued == []


def test_a_worker_that_edited_its_creator_away_is_not_resumed(world):
    worker = world.state.get_or_create_slot("chat-own-worker")
    world.store.record_worker(worker.key, world.slot.key)
    worker._created_by = ""
    _interrupt(worker)
    _restart(world)
    assert world.continued == []


@pytest.mark.parametrize("gate", ["workspace", "channel", "governance"])
def test_a_channel_the_gates_refuse_is_neither_resumed_nor_posted(world, monkeypatch, gate):
    if gate == "workspace":
        monkeypatch.setattr(cc_surface, "workspace_admitted", lambda team: False)
    elif gate == "channel":
        monkeypatch.setattr(routing, "channel_and_origin_permit", lambda cfg, c, o: False)
    else:
        monkeypatch.setattr(cc_surface, "outbound_permitted", AsyncMock(return_value=False))
    _interrupt(world.slot)
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []


def test_an_old_button_does_not_cover_a_newer_turn(world):
    ts, button = _held_notice(world)
    _interrupt(world.slot, ts="2026-10-19T11:00:00+00:00")  # a new request, also cut off
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    # The owner's press means "resume what is interrupted here now", as the
    # dashboard's Resume does; the turn it resumed is then recorded, so a later
    # restart holds it for the button instead of resuming it automatically.
    assert world.continued == [world.slot.key, world.slot.key]
    assert world.store.resume_record(world.slot.key)["turn"] == "2026-10-19T11:00:00+00:00"


def test_two_deliveries_of_one_press_continue_once(world):
    ts, button = _held_notice(world)

    async def both() -> None:
        await asyncio.gather(
            resume.handle_resume_click(None, _click(OWNER, ts, button)),
            resume.handle_resume_click(None, _click(OWNER, ts, button)),
        )

    asyncio.run(both())
    assert world.continued == [world.slot.key, world.slot.key]


def test_a_lost_post_tells_the_owner_in_the_dashboard_with_links(world, monkeypatch):
    from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce

    notes: list[tuple] = []
    world.state.notify = lambda *a, **k: notes.append(a)
    monkeypatch.setattr(
        cce,
        "_live_config",
        lambda: SimpleNamespace(
            slack=SimpleNamespace(use_tunnel_url=False),
            dashboard=SimpleNamespace(url="http://127.0.0.1:7777"),
        ),
    )
    monkeypatch.setattr(world.slack, "post_blocks", AsyncMock(side_effect=TimeoutError()))
    _interrupt(world.slot)
    _restart(world)
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    _restart(world)  # held: its notice could not be posted
    assert notes, "the owner is told in the dashboard"
    body = notes[-1][2]
    assert "http://127.0.0.1:7777/chat?sid=" in body and "token" not in body
    assert f"- [{world.slot.title or world.slot.key}](http://127.0.0.1:7777/chat?sid=" in body


def test_nothing_happens_while_code_channels_are_off(world, monkeypatch):
    monkeypatch.setattr(resume, "code_channels_on", lambda cfg: False)
    _interrupt(world.slot)
    assert _restart(world) == []
    assert world.continued == [] and world.slack.posts == []
    payload = {"channel": {"id": CHANNEL}, "user": {"id": OWNER}, "actions": [{}]}
    assert asyncio.run(resume.handle_resume_click(None, payload)) is False


# ── round-16 verifier ──────────────────────────────────────────────────────


def _real_continue(world, monkeypatch, meanwhile):
    """Undo the fixture's fake continue; run the real one, with *meanwhile*
    applied during its awaited sub-agent probe. Returns the turn-start mock."""
    import kiro_crew.dashboard.chat_handlers as ch

    feature = {"on": True}
    governance = {"ok": True}
    monkeypatch.setattr(resume, "_continue", _REAL_CONTINUE)
    monkeypatch.setattr(resume, "code_channels_on", lambda cfg: feature["on"])

    async def _governance() -> bool:
        return governance["ok"]

    monkeypatch.setattr(cc_surface, "outbound_permitted", _governance)
    started = AsyncMock(return_value=True)
    monkeypatch.setattr(ch, "_start_next_queued_turn", started)
    monkeypatch.setattr(ch, "sel", lambda: MagicMock())

    async def _probe(*_a, **_kw):
        meanwhile(world, feature, governance)
        return None

    monkeypatch.setattr(ch, "_subagents_attached_response", _probe)
    return started


@pytest.mark.parametrize(
    "meanwhile",
    [
        pytest.param(
            lambda w, f, g: w.store.update_binding(CHANNEL, wrapped_up_at=1.0),
            id="wrapped-up",
        ),
        pytest.param(lambda w, f, g: f.update(on=False), id="code-channels-off"),
        pytest.param(lambda w, f, g: g.update(ok=False), id="governance-denies"),
    ],
)
def test_a_revocation_during_the_continue_stops_it(world, monkeypatch, meanwhile):
    """Through the real continue: what changes while it awaits its child probe
    is re-checked before the queue insert."""
    started = _real_continue(world, monkeypatch, meanwhile)
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.held == [world.slot.key] and report.resumed == []
    assert world.slot.queue_depth == 0
    started.assert_not_awaited()


def test_the_real_continue_resumes_when_nothing_changes(world, monkeypatch):
    _real_continue(world, monkeypatch, lambda w, f, g: None)
    _interrupt(world.slot)
    (report,) = _restart(world)
    assert report.resumed == [world.slot.key]


def test_a_mirrored_worker_is_not_resumed(world, monkeypatch):
    from kiro_crew.dashboard import session_control

    worker = world.state.get_or_create_slot("chat-mirrored-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    _interrupt(worker)
    monkeypatch.setattr(
        session_control, "_has_channel_mirror", lambda state, slot, **kw: slot is worker
    )
    _restart(world)
    assert worker.key not in world.continued


def test_a_partial_press_keeps_the_button_for_the_rest(world, monkeypatch):
    worker = world.state.get_or_create_slot("chat-held-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    ts, button = _held_notice(world)
    _interrupt(worker)

    async def _one_refuses(state, slot, still_ok=None, **_kw):
        return "slot_queue_pending" if slot is worker else ""

    monkeypatch.setattr(resume, "_continue", _one_refuses)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert f"{worker.key} could not be resumed right now" in world.slack.ephemerals[-1][2]
    _assert_not_done(world)
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


def test_a_same_key_slot_not_linked_to_the_channel_is_not_resumed(world):
    """Key equality alone is not identity: the slot must also be the channel's."""
    _interrupt(world.slot)
    world.slot._slack_channel = "C0ELSEWHERE"
    assert _restart(world) == []
    assert world.continued == []


def test_a_crew_log_only_worker_is_resumed(world, monkeypatch):
    from kiro_crew.dashboard.session_control_parts import session_status

    worker = world.state.get_or_create_slot("chat-tree-worker")
    worker._created_by = world.slot.key
    _interrupt(worker)
    monkeypatch.setattr(
        session_status,
        "_created_tree_roster",
        lambda key: (
            (["chat-tree-worker"], "readable") if key == world.slot.key else ([], "readable")
        ),
    )
    _restart(world)
    assert world.continued == [worker.key]


def test_a_press_that_resumes_nothing_gives_the_button_back(world, monkeypatch):
    ts, button = _held_notice(world)
    monkeypatch.setattr(resume, "_continue", AsyncMock(return_value="slot_queue_pending"))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert "messages are waiting in that session's queue" in world.slack.ephemerals[-1][2]
    assert "slot_queue_pending" not in world.slack.ephemerals[-1][2], "no raw refusal code"
    _assert_not_done(world)
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


def test_a_contended_session_map_refuses_the_worker_without_waiting(world):
    """The final mirror re-check runs on the event loop under the slot lock: a
    session-map lock held by an off-loop writer makes it refuse, never wait."""
    import threading

    from kiro_crew.session_map import _MAP_LOCK

    worker = world.state.get_or_create_slot("chat-busy-map-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    held, release = threading.Event(), threading.Event()

    def _writer() -> None:
        with _MAP_LOCK:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=_writer)
    thread.start()
    try:
        assert held.wait(5)
        assert resume._worker_mirror(world.state, worker) == resume.UNVERIFIED
        check = resume._recheck_for(world.state, CHANNEL, world.slot, worker, resume.UNKNOWN_TURN)
        assert check() == "worker mirror unverified"
    finally:
        release.set()
        thread.join(5)
    assert resume._worker_mirror(world.state, worker) == ""


@pytest.mark.parametrize("who", ["conductor", "worker"])
def test_a_busy_session_map_refuses_the_real_continue_at_once(world, monkeypatch, who):
    """Through the real continue: the containment snapshot and insert run under a
    TRIED session-map lock, so a writer holding it costs a refusal, not a stall."""
    import threading

    from kiro_crew.session_map import _MAP_LOCK

    target = world.slot
    if who == "worker":
        target = world.state.get_or_create_slot("chat-busy-real-worker")
        target._created_by = world.slot.key
        world.store.record_worker(target.key, world.slot.key)
    held, release = threading.Event(), threading.Event()

    def _writer() -> None:
        with _MAP_LOCK:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=_writer)

    def _meanwhile(w, f, g) -> None:
        if not thread.is_alive():
            thread.start()
            assert held.wait(5)

    started = _real_continue(world, monkeypatch, _meanwhile)
    _interrupt(target)
    try:
        # Had the guard waited, it would have waited out the writer and resumed.
        (report,) = _restart(world)
    finally:
        release.set()
        thread.join(5)
    assert target.key in report.held and target.key not in report.resumed
    assert target.queue_depth == 0
    started.assert_not_awaited()


def test_a_resumed_conductor_turn_keeps_channel_authority(world, monkeypatch):
    """Through the real continue: the conductor's recovery entry is channel text,
    so the dispatch exemption's sender check governs it (after a restart no author
    is on record, so it hands off nothing); a worker's is not channel text."""
    from kiro_crew.dashboard.session_control_parts.code_channel_exemption import (
        _sender_refusal,
    )

    worker = world.state.get_or_create_slot("chat-authority-worker")
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    _real_continue(world, monkeypatch, lambda w, f, g: None)
    _interrupt(world.slot)
    _interrupt(worker)
    (report,) = _restart(world)
    assert sorted(report.resumed) == sorted([world.slot.key, worker.key])
    (entry,) = world.slot._queue
    assert entry.get("_directive_channel_origin") is True
    (worker_entry,) = worker._queue
    assert worker_entry.get("_directive_channel_origin") is not True
    world.slot._turn_channel_origin = True  # what the drain sets from that entry
    assert _sender_refusal(world.slot), "no author on record after a restart"


def test_a_pressed_resume_keeps_the_conductors_channel_authority(world, monkeypatch):
    """The owner's press continues the channel's turn as channel text too."""
    ts, button = _held_notice(world)
    _real_continue(world, monkeypatch, lambda w, f, g: None)
    monkeypatch.setattr(cc_surface, "dashboard_state", lambda: world.state)
    monkeypatch.setattr(slack_handler, "is_owner", lambda user: user == OWNER)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    (entry,) = world.slot._queue
    assert entry.get("_directive_channel_origin") is True


def test_a_press_records_the_turn_before_it_runs(world, monkeypatch):
    ts, button = _held_notice(world)
    _interrupt(world.slot, ts="2026-10-19T11:00:00+00:00")
    seen: list[Any] = []

    async def _check(state_: Any, target: Any, still_ok: Any = None, **_kw: Any) -> str:
        seen.append(world.store.resume_record(target.key))
        return ""

    monkeypatch.setattr(resume, "_continue", _check)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert seen and seen[0]["turn"] == "2026-10-19T11:00:00+00:00"


def test_a_press_whose_turn_cannot_be_recorded_runs_nothing(world, monkeypatch):
    ts, button = _held_notice(world)
    _interrupt(world.slot, ts="2026-10-19T11:00:00+00:00")
    before = list(world.continued)

    def _fail(*a: Any, **k: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(world.store, "record_resume", _fail)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.continued == before
    assert "the record that it was resumed couldn't be saved" in world.slack.ephemerals[-1][2]
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


# ── UX round: the automatic notice's Stop button, and why work was held ────


def _auto_notice(world, *workers: Any) -> tuple[str, dict]:
    _interrupt(world.slot)
    for worker in workers:
        _interrupt(worker)
    _restart(world)
    ts = f"1700000000.00000{len(world.slack.posts)}"
    return ts, world.slack.posts[-1][1][-1]["elements"][0]


@pytest.fixture
def stops(monkeypatch):
    """Record ``stop_slot_turn`` calls: the dashboard Stop control's own path."""
    from kiro_crew.dashboard import chat_handlers

    calls: list[tuple[str, dict]] = []

    async def _stop(state_: Any, slot: Any, **kw: Any) -> dict:
        calls.append((slot.key, kw))
        return {"info": "stopping"}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _stop)
    return calls


def _own_worker(world, key: str = "chat-own-worker") -> Any:
    worker = world.state.get_or_create_slot(key)
    worker._created_by = world.slot.key
    world.store.record_worker(worker.key, world.slot.key)
    return worker


def test_the_owners_stop_stops_what_the_restart_resumed(world, stops):
    worker = _own_worker(world)
    ts, button = _auto_notice(world, worker)
    world.slot._turn_admission_reserved = True
    worker._turn_admission_reserved = True
    assert asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button))) is True
    assert sorted(k for k, _ in stops) == sorted([world.slot.key, worker.key])
    assert all(kw == {"source": "slack", "escalate": False} for _, kw in stops)
    _, updated_ts, text, blocks = world.slack.updates[-1]
    assert updated_ts == ts and text == resume.AUTO_TEXT
    assert not any(b["type"] == "actions" for b in blocks), "the button is spent"
    assert resume.STOPPED_TEXT.format(user=OWNER) in json.dumps(blocks)
    stop_audits = [a for a in world.audits if a[1] == resume.STOP_OPERATION]
    assert {a[0] for a in stop_audits} == {OWNER}
    assert sum("result=stopping" in a[3] for a in stop_audits) == 2
    # Single use: a second press stops nothing.
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert len(stops) == 2
    assert world.slack.ephemerals[-1] == (CHANNEL, OWNER, resume.STOP_STALE_TEXT)


def test_the_stop_button_is_owner_only(world, stops):
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    asyncio.run(resume.handle_stop_click(None, _click(OTHER, ts, button)))
    assert stops == []
    assert world.slack.ephemerals[-1] == (
        CHANNEL,
        OTHER,
        resume.STOP_NOT_OWNER_TEXT.format(owner=cc_surface.owner_name()),
    )
    assert any(
        a[0] == OTHER and a[1] == resume.STOP_OPERATION and a[2] == "denied" for a in world.audits
    )
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "auto", "still pressable"


def test_a_stale_or_forged_stop_is_refused(world, stops):
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    forged = dict(button, value=json.dumps({"channel": "C0SOMEWHERE"}))
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, forged)))
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, "1700000099.000001", button)))
    assert stops == []
    assert world.slack.ephemerals[-2:] == [
        (CHANNEL, OWNER, resume.STOP_NOT_ALLOWED_TEXT),
        (CHANNEL, OWNER, resume.STOP_STALE_TEXT),
    ]


def test_stop_never_reaches_a_session_that_is_no_longer_the_channels(world, stops):
    worker = _own_worker(world)
    ts, button = _auto_notice(world, worker)
    worker._turn_admission_reserved = True
    worker._created_by = "chat-another-conductor"  # handed to someone else since
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert stops == []
    assert resume.STOPPED_IDLE_TEXT.format(user=OWNER) in json.dumps(world.slack.updates[-1][3])


def test_stop_on_finished_work_stops_nothing_and_says_so(world, stops):
    ts, button = _auto_notice(world)
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert stops == []
    assert resume.STOPPED_IDLE_TEXT.format(user=OWNER) in json.dumps(world.slack.updates[-1][3])


def test_the_router_dispatches_the_stop_prefix(world, monkeypatch, stops):
    from kiro_crew.slack import interactions

    monkeypatch.setattr(interactions, "is_allowed_user", lambda user: True)
    monkeypatch.setattr(interactions, "channel_inbound_permitted", AsyncMock(return_value=True))
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    asyncio.run(interactions.dispatch({"type": "block_actions", **_click(OWNER, ts, button)}))
    assert [k for k, _ in stops] == [world.slot.key]


def test_a_held_notice_has_no_stop_and_the_auto_notice_no_resume(world):
    _interrupt(world.slot)
    _restart(world)
    world.slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )
    _restart(world)
    auto, held = (post[1][-1]["elements"] for post in world.slack.posts)
    assert [b["action_id"][: len(resume.STOP_ACTION_PREFIX)] for b in auto] == [
        resume.STOP_ACTION_PREFIX
    ]
    assert [b["action_id"][: len(resume.RESUME_ACTION_PREFIX)] for b in held] == [
        resume.RESUME_ACTION_PREFIX
    ]


def test_a_held_notice_says_it_could_not_save_the_record(world, monkeypatch):
    def _fail(*a: Any, **k: Any) -> None:
        raise OSError("read-only")

    _interrupt(world.slot)
    monkeypatch.setattr(world.store, "record_resume", _fail)
    _restart(world)
    assert resume.HELD_UNRECORDED in _texts(world)[0]
    assert world.continued == []


def test_a_held_notice_names_each_distinct_reason_once(world):
    worker = _own_worker(world)
    _interrupt(world.slot)
    _interrupt(worker)
    _restart(world)
    for slot in (world.slot, worker):
        slot.messages.append(
            {
                "role": "inject",
                "content": "resume",
                "ts": "2026-10-19T10:05:00+00:00",
                "meta": {"injectKind": "recovery"},
            }
        )
    _restart(world)
    assert _texts(world)[-1] == resume.held_text([resume.HELD_ALREADY])
    assert _texts(world)[-1].count(resume.HELD_ALREADY) == 1


def test_the_pressed_notice_keeps_its_reason(world):
    ts, button = _held_notice(world)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    _, _, text, blocks = world.slack.updates[-1]
    assert text == resume.held_text([resume.HELD_ALREADY])
    assert blocks[0]["text"]["text"] == text


# ── fresh-context review: turn scope, failures, mixed notices, races ───────


def test_stop_leaves_a_newer_turn_alone(world, stops):
    ts, button = _auto_notice(world)
    _interrupt(world.slot, ts="2026-10-19T12:00:00+00:00")  # the owner's later task
    world.slot._turn_admission_reserved = True
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert stops == []
    assert any(a[1] == resume.STOP_OPERATION and "a newer turn" in a[4] for a in world.audits)


def test_a_failed_stop_gives_the_button_back(world, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    monkeypatch.setattr(chat_handlers, "stop_slot_turn", AsyncMock(side_effect=RuntimeError()))
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert world.slack.ephemerals[-1][2].startswith("I couldn't stop the resumed work")
    _assert_not_done(world)
    assert world.store.resume_record(f"notice:{CHANNEL}").get("stop") != "pressed"
    assert any(a[1] == resume.STOP_OPERATION and a[2] == "error" for a in world.audits)
    calls: list[str] = []

    async def _ok(state_: Any, slot: Any, **kw: Any) -> dict:
        calls.append(slot.key)
        return {"info": "stopping"}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", _ok)
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert calls == [world.slot.key], "the retry stops it"


def test_an_unexpected_error_after_the_claim_gives_the_button_back(world, monkeypatch, stops):
    ts, button = _auto_notice(world)
    monkeypatch.setattr(resume, "_stop_resumed", AsyncMock(side_effect=RuntimeError()))
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert world.store.resume_record(f"notice:{CHANNEL}").get("stop") != "pressed"
    assert world.slack.ephemerals[-1][2].startswith("I couldn't stop the resumed work")


def _mixed_notice(world) -> tuple[str, list[dict]]:
    """The conductor resumed, its worker held (already restarted once)."""
    worker = _own_worker(world)
    _interrupt(worker)
    _restart(world)  # the worker resumes once
    _recover(worker)
    _interrupt(world.slot, ts="2026-10-19T11:00:00+00:00")  # the conductor, fresh
    _restart(world)
    ts = f"1700000000.00000{len(world.slack.posts)}"
    return ts, world.slack.posts[-1][1]


def _recover(slot: Any) -> None:
    slot.messages.append(
        {
            "role": "inject",
            "content": "resume",
            "ts": "2026-10-19T10:05:00+00:00",
            "meta": {"injectKind": "recovery"},
        }
    )


def test_a_restart_that_resumed_and_held_offers_both_buttons(world, stops):
    world.slot.title = "Billing retry fix"
    world.state.get_or_create_slot("chat-own-worker").title = "Retry backoff tests"
    ts, blocks = _mixed_notice(world)
    assert "I picked up part of this channel's work" in blocks[0]["text"]["text"]
    # Each button sits on the line naming the work it acts on.
    picked, paused = blocks[1], blocks[2]
    assert picked["text"]["text"] == "*Picked up:* Billing retry fix"
    assert picked["accessory"]["action_id"].startswith(resume.STOP_ACTION_PREFIX)
    assert paused["text"]["text"] == "*Paused:* Retry backoff tests"
    assert paused["accessory"]["action_id"].startswith(resume.RESUME_ACTION_PREFIX)
    world.slot._turn_admission_reserved = True
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, picked["accessory"])))
    assert [k for k, _ in stops] == [world.slot.key], "only what the restart resumed"
    view = world.slack.updates[-1][3]
    assert "accessory" not in view[1], "Stop is spent"
    assert view[2]["accessory"]["action_id"].startswith(resume.RESUME_ACTION_PREFIX), "Resume stays"
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button"


def test_two_deliveries_of_one_stop_stop_once(world, stops):
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True

    async def both() -> None:
        await asyncio.gather(
            resume.handle_stop_click(None, _click(OWNER, ts, button)),
            resume.handle_stop_click(None, _click(OWNER, ts, button)),
        )

    asyncio.run(both())
    assert [k for k, _ in stops] == [world.slot.key]


def test_a_stop_from_another_workspace_does_nothing(world, stops):
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    click = {**_click(OWNER, ts, button), "team": {"id": "T0ELSEWHERE"}}
    assert asyncio.run(resume.handle_stop_click(None, click)) is True
    assert stops == [] and world.slack.ephemerals == []


def test_stop_never_reaches_a_rebound_conductor(world, stops):
    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    world.slot._slack_channel = "C0ELSEWHERE"  # no longer the channel's session
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert stops == []


# ── round on ed6d3b0d71: compaction, already running, unit, overlay state ─


def test_a_stop_declined_by_compaction_gives_the_button_back(world, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    ts, button = _auto_notice(world)
    world.slot._turn_admission_reserved = True
    monkeypatch.setattr(
        chat_handlers,
        "stop_slot_turn",
        AsyncMock(return_value={"ok": True, "info": "compacting", "compacting": True}),
    )
    asyncio.run(resume.handle_stop_click(None, _click(OWNER, ts, button)))
    assert "compacting" in world.slack.ephemerals[-1][2]
    _assert_not_done(world)
    assert world.store.resume_record(f"notice:{CHANNEL}").get("stop") != "pressed"


def test_work_already_running_is_not_a_failed_resume(world, monkeypatch):
    ts, button = _held_notice(world)
    monkeypatch.setattr(resume, "_continue", AsyncMock(return_value="slot_running"))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    assert world.slack.ephemerals == [], "no retry advice for running work"
    view = world.slack.updates[-1][3]
    assert resume.ALREADY_RUNNING_TEXT.format(user=OWNER) in json.dumps(view)


def test_the_partial_reply_names_its_unit(world, monkeypatch):
    worker = _own_worker(world, "chat-held-worker-2")
    ts, button = _held_notice(world)
    _interrupt(worker)

    async def _one_refuses(state, slot, still_ok=None, **_kw):
        return "slot_queue_pending" if slot is worker else ""

    monkeypatch.setattr(resume, "_continue", _one_refuses)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    world_text = world.slack.ephemerals[-1][2]
    # Named by title, as the notice names them, not counted.
    assert world_text.startswith(
        f"Resumed {world.slot.title or world.slot.key}; {worker.key} could"
    )
    assert world_text.endswith(f"Press Resume again in a moment for {worker.key}.")


def test_owner_refusals_use_a_slack_shortcode_not_a_raw_glyph():
    for text in (
        resume.NOT_OWNER_TEXT.format(owner=cc_surface.owner_name()),
        resume.STOP_NOT_OWNER_TEXT.format(owner=cc_surface.owner_name()),
        resume.NOT_ALLOWED_TEXT,
    ):
        assert text.startswith(":no_entry: ") and "\u26d4" not in text


def test_an_approval_wait_links_the_session_and_does_not_say_retry(world, monkeypatch):
    from kiro_crew.dashboard import session_control

    # Through the facade: the link helper is shared by more than one owner.
    monkeypatch.setattr(
        session_control,
        "worker_dashboard_link",
        lambda key: f"http://127.0.0.1:7777/chat?sid={key}",
    )
    world.slot.title = "Billing retry fix"
    ts, button = _held_notice(world)
    monkeypatch.setattr(resume, "_continue", AsyncMock(return_value="slot_approval_pending"))
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    text = world.slack.ephemerals[-1][2]
    assert f"<http://127.0.0.1:7777/chat?sid={world.slot.key}|Billing retry fix>" in text
    assert "in a moment" not in text and "Approve or decline it there" in text
    assert world.store.resume_record(f"notice:{CHANNEL}")["state"] == "button", "still pressable"


# ── GPT on ab90c3719f: a concurrent press on the other button ─────────────


def test_a_resume_refusal_after_a_stop_meanwhile_keeps_both_states(world, monkeypatch, stops):
    """Stop changes the record while Resume runs: Resume gives back only its own claim."""
    ts, blocks = _mixed_notice(world)
    stop_btn, resume_btn = blocks[1]["accessory"], blocks[2]["accessory"]

    async def _stop_meanwhile(state, slot, still_ok=None, **_kw):
        world.slot._turn_admission_reserved = True
        await resume.handle_stop_click(None, _click(OWNER, ts, stop_btn))
        return "slot_queue_pending"

    monkeypatch.setattr(resume, "_continue", _stop_meanwhile)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, resume_btn)))
    record = world.store.resume_record(f"notice:{CHANNEL}")
    assert record["state"] == "button", "Resume is pressable again"
    assert record["stop"] == "pressed", "the Stop that ran is kept"
    assert "Resume again in a moment" in world.slack.ephemerals[-1][2]


def test_a_lost_claim_does_not_advertise_a_retry(world, monkeypatch):
    ts, button = _held_notice(world)

    async def _replaced_meanwhile(state, slot, still_ok=None, **_kw):
        record = world.store.resume_record(f"notice:{CHANNEL}")
        world.store.record_resume(f"notice:{CHANNEL}", {**record, "notice_ts": "1700000099.000009"})
        return "slot_queue_pending"

    monkeypatch.setattr(resume, "_continue", _replaced_meanwhile)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    text = world.slack.ephemerals[-1][2]
    assert "can't be offered again" in text and "Press Resume again" not in text


def test_button_labels_name_their_sessions_and_stay_within_slacks_limit(world):
    assert len(resume._label("Stop", world.state, [world.slot.key])) <= 75
    world.slot.title = "x" * 120
    assert resume._label("Resume", world.state, [world.slot.key]).endswith("…")
    assert resume._label("Stop", world.state, ["a", "b"]) == "Stop 2 sessions"


def test_the_trust_note_is_the_same_on_the_paused_notice(world):
    world.slot._trust = True
    _interrupt(world.slot)
    _restart(world)
    _recover(world.slot)
    _restart(world)
    auto, held = (post[1][0]["text"]["text"] for post in world.slack.posts)
    assert auto.endswith(resume.TRUST_NOTE) and held.endswith(resume.TRUST_NOTE)


def test_concurrent_presses_never_hide_the_button_a_retry_names(world, monkeypatch, stops):
    """Fresh-context review: Stop lands while Resume holds its claim; Resume then
    fails and gives the claim back. The message must show Resume again (it is the
    button the reply says to press), next to Stop's note, and Stop stays spent."""
    ts, blocks = _mixed_notice(world)
    stop_btn, resume_btn = blocks[1]["accessory"], blocks[2]["accessory"]

    async def _stop_meanwhile(state, slot, still_ok=None, **_kw):
        world.slot._turn_admission_reserved = True
        await resume.handle_stop_click(None, _click(OWNER, ts, stop_btn))
        return "slot_queue_pending"

    monkeypatch.setattr(resume, "_continue", _stop_meanwhile)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, resume_btn)))
    last = world.slack.updates[-1][3]
    assert "accessory" not in last[1], "Stop is spent"
    assert last[2]["accessory"]["action_id"].startswith(resume.RESUME_ACTION_PREFIX)
    assert "Stopped by" in json.dumps(last)


def test_a_lost_claim_with_nothing_resumed_says_nothing_resumed(world, monkeypatch):
    ts, button = _held_notice(world)

    async def _replaced_meanwhile(state, slot, still_ok=None, **_kw):
        record = world.store.resume_record(f"notice:{CHANNEL}")
        world.store.record_resume(f"notice:{CHANNEL}", {**record, "notice_ts": "1700000099.000009"})
        return "slot_queue_pending"

    monkeypatch.setattr(resume, "_continue", _replaced_meanwhile)
    asyncio.run(resume.handle_resume_click(None, _click(OWNER, ts, button)))
    text = world.slack.ephemerals[-1][2]
    assert text.startswith("Nothing could be resumed") and "0 sessions" not in text


def test_a_label_keeps_a_title_that_spells_an_entity(world):
    world.slot.title = "fix &lt;tag&gt; parsing"
    assert resume._label("Stop", world.state, [world.slot.key]) == "Stop fix &lt;tag&gt; parsing"


def test_refusals_name_the_owner(world, monkeypatch):
    """The named owner is the one ``is_owner`` checks: the real check, not a stub."""
    monkeypatch.setattr(slack_handler, "_owner_id", "")
    slack_handler.set_owner_id(OWNER)
    from kiro_crew.slack.handler_runtime import access

    monkeypatch.setattr(slack_handler, "is_owner", access.is_owner)
    ts, button = _held_notice(world)
    asyncio.run(resume.handle_resume_click(None, _click(OTHER, ts, button)))
    assert world.slack.ephemerals[-1][2] == (
        f":no_entry: Only the Kiro Crew owner (<@{OWNER}>) can resume this channel's work."
    )


def test_the_trust_note_says_what_happens_on_resume_in_both_notices():
    assert "once work resumes" in resume.TRUST_NOTE and "carries on" not in resume.TRUST_NOTE
    for held in (False, True):
        blocks = resume.notice_blocks("C0X", "k@t", held=held, trust=True, reasons=["r"])
        assert blocks[0]["text"]["text"].endswith(resume.TRUST_NOTE)


def test_the_stop_refusal_names_the_owner(world, monkeypatch, stops):
    monkeypatch.setattr(slack_handler, "_owner_id", OWNER)
    ts, button = _auto_notice(world)
    asyncio.run(resume.handle_stop_click(None, _click(OTHER, ts, button)))
    assert world.slack.ephemerals[-1][2] == (
        f":no_entry: Only the Kiro Crew owner (<@{OWNER}>) can stop this channel's resumed work."
    )


def test_a_malformed_owner_id_is_never_turned_into_a_mention(monkeypatch):
    monkeypatch.setattr(slack_handler, "_owner_id", "U123>|<!channel")
    assert cc_surface.owner_name() == "the Kiro Crew owner"
    monkeypatch.setattr(slack_handler, "_owner_id", "W0ABC123")
    assert cc_surface.owner_name() == "the Kiro Crew owner (<@W0ABC123>)"


def test_the_post_memory_start_schedules_the_resume_only_with_a_slack_client(monkeypatch):
    """With Slack the pass is scheduled once; an orchestrator built without a
    ``slack`` attribute (main's ``object.__new__`` stubs) skips it instead of raising."""
    from kiro_crew.slack.gateway_runtime import admission

    seen: list[object] = []

    async def fake_resume(state, connected):
        seen.append(state)

    monkeypatch.setattr(resume, "resume_when_ready", fake_resume)

    async def run(gateway) -> None:
        admission._start_dashboard_workers_after_memory_ready(gateway)
        await asyncio.sleep(0)

    bare = SimpleNamespace(dashboard_state=SimpleNamespace(resume_channel_agents=None))
    asyncio.run(run(bare))
    assert seen == []

    state = SimpleNamespace(resume_channel_agents=None)
    with_slack = SimpleNamespace(dashboard_state=state, slack=object(), _background_tasks=set())
    asyncio.run(run(with_slack))
    assert seen == [state]
