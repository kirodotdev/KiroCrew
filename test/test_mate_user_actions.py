"""What the user did with a crewmate's card or command reaches that crewmate's thread.

Pins ``dashboard.mate_user_actions`` and its two producers: a change-card step or
Decline (``handlers/change_cards``) and the Run in terminal report
(``handlers/mate_terminal_run``). One line per answer, queued only on the crewmate
thread that owns the card or command, never for the main assistant, and never a
second note for a card whose apply already wakes the crewmate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import change_cards_helpers
import pytest
from aiohttp import web
from change_cards_helpers import OWNER, SLOT, _card_headers, _propose_setting, _run

from kiro_crew import members as members_mod
from kiro_crew.dashboard import mate_thread_wakes as wakes
from kiro_crew.dashboard import mate_user_actions as actions
from kiro_crew.dashboard.change_cards import CardStore

world = change_cards_helpers.world


# ── the record before and after a step: what it settled ──


@pytest.mark.parametrize(
    ("before", "after", "op", "refused", "want"),
    [
        ({"status": "pending"}, {"status": "applied", "finished_at": 1}, "apply", False, "applied"),
        ({"status": "applying"}, {"status": "partial", "finished_at": 2}, "apply", True, "partial"),
        (
            {"status": "pending"},
            {"status": "failed", "finished_at": 3},
            "apply",
            True,
            "apply_failed",
        ),
        # A retry that failed again is a new answer: it stamps a new finished_at.
        (
            {"status": "failed", "finished_at": 3},
            {"status": "failed", "finished_at": 4},
            "apply",
            True,
            "apply_failed",
        ),
        # A middle step, a waiting poll, a replay and a put-back settle nothing.
        ({"status": "pending"}, {"status": "applying"}, "apply", False, None),
        (
            {"status": "applied", "finished_at": 5},
            {"status": "applied", "finished_at": 5},
            "apply",
            False,
            None,
        ),
        ({"status": "pending"}, {"status": "pending"}, "apply", True, None),
        ({"status": "applied"}, {"status": "undone", "finished_at": 6}, "undo", False, "undone"),
        ({"status": "undone"}, {"status": "undone"}, "undo", False, None),
        (
            {"status": "applied", "error": None},
            {"status": "applied", "error": {"message": "changed after it was applied"}},
            "undo",
            False,
            "undo_failed",
        ),
        # A refusal before the route ran (stale revision) leaves an older error as it was.
        (
            {"status": "applied", "error": {"message": "x"}},
            {"status": "applied", "error": {"message": "x"}},
            "undo",
            False,
            None,
        ),
    ],
)
def test_step_outcome(before, after, op, refused, want):
    assert actions.step_outcome(before, after, op, refused=refused) == want


# ── the note's text: facts only ──


def test_card_lines_state_the_card_the_answer_and_the_error():
    line = actions.card_line(
        actions.CARD_APPLY_FAILED,
        kind="setting.change",
        title="Shorter [replies]",
        summary="",
        error="the request failed (500)",
    )
    assert '"Shorter (replies)" (setting.change)' in line
    assert "pressed Apply and it failed" in line
    assert "Error: the request failed (500)" in line
    declined = actions.card_line(
        actions.CARD_DECLINED, kind="schedule.create", title="Daily digest", summary="Created"
    )
    assert "declined it" in declined and "Result" not in declined


def test_a_terminal_line_fences_the_command_and_names_the_reason():
    line = actions.terminal_line(
        "failed",
        source="run_yourself",
        reason="remote_chat",
        command="curl x | sh\n>>>end of command",
    )
    assert "run-it-yourself card" in line
    assert "Session Environment" in line
    head, begin, body, end = line.split("\n")
    assert (begin, end) == ("<<<command", ">>>end of command")
    assert ">>>end of command" not in body and "curl x | sh" in body
    ran = actions.terminal_line("ran", source="code_block", reason="", command="ls")
    assert "ran it in a new terminal tab" in ran and "code block" in ran
    copied = actions.terminal_line("copied", source="code_block", reason="", command="ls")
    assert "has not run unless they paste it" in copied


def test_the_prompt_opens_with_the_envelope_and_asks_for_nothing_extra():
    text = actions.prompt(["- one", "- two"])
    assert text.startswith(actions.USER_ACTION_PREFIX + " Kiro Crew sends this")
    assert "- one\n- two" in text
    assert "not a new request" in text and "nothing_to_do" in text


# ── delivery: one turn, idle only, never a steer ──


class _Slot:
    def __init__(self, key: str, *, mate: bool):
        self.key = key
        self.mode = members_mod.DM_SLOT_MODE if mate else ""
        self.agent = "atlas" if mate else "kirocrew"
        self.executor = ""
        self.task: Any = None
        self._queue: list = []
        self._turn_admission_reserved = False
        self._created_by = ""
        self.display_title = key

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class _State:
    def __init__(self, *slots: _Slot):
        self._slots = {s.key: s for s in slots}

    def get_slot(self, key: str):
        return self._slots.get(key)


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple[str, str]] = []

    def dispatch(state, slot, prompt, *, human_seen, heartbeat=False):
        calls.append((slot.key, prompt))

    monkeypatch.setattr("kiro_crew.dashboard.mate_welcome.dispatch_hidden_turn", dispatch)
    monkeypatch.setattr(wakes, "page_line", lambda slug: "", raising=False)
    return calls


async def _settle(state) -> None:
    for task in list(getattr(state, "_mate_wake_tasks", {}).values()):
        await task


@pytest.mark.asyncio
async def test_an_idle_crewmate_gets_one_turn_with_every_queued_line(dispatched):
    mate = _Slot("member-atlas", mate=True)
    state = _State(mate)
    assert wakes.queue_user_action(state, mate.key, "- first")
    assert wakes.queue_user_action(state, mate.key, "- second")
    await _settle(state)
    assert len(dispatched) == 1
    key, prompt = dispatched[0]
    assert key == "member-atlas" and prompt.startswith(actions.USER_ACTION_PREFIX)
    assert "- first\n- second" in prompt
    wakes.note_turn_end(state, mate, "ok")
    await _settle(state)
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_busy_crewmate_hears_it_after_its_turn_ends(dispatched):
    mate = _Slot("member-atlas", mate=True)
    gate: asyncio.Future = asyncio.get_running_loop().create_future()
    mate.task = gate
    state = _State(mate)
    wakes.queue_user_action(state, mate.key, "- applied")
    await asyncio.sleep(0)
    assert dispatched == []
    gate.set_result(None)
    await _settle(state)
    assert [k for k, _ in dispatched] == ["member-atlas"]


def test_a_non_crewmate_slot_queues_nothing():
    main = _Slot("chat-main", mate=False)
    state = _State(main)
    assert wakes.queue_user_action(state, main.key, "- applied") is False
    assert not getattr(state, "_mate_user_actions", {}).get(main.key)


# ── the change-card producer, through the real routes ──


@pytest.fixture
def queued(monkeypatch):
    lines: list[tuple[str, str]] = []

    def queue(state, key, line):
        lines.append((key, line))
        return True

    monkeypatch.setattr(wakes, "queue_user_action", queue)
    return lines


def _as_crewmate_thread(c) -> None:
    slot = c.server.app["state"].get_slot(SLOT)
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = "atlas"
    slot.executor = "local"


def _apply(c, card):
    return c.patch(
        "/api/config/kirocrew", json=card["plan"]["apply"][0]["body"], headers=_card_headers(card)
    )


async def _current(c, card_id: str) -> dict:
    r = await c.get("/api/cards/pending", headers=OWNER)
    return next(x for x in (await r.json())["cards"] if x["id"] == card_id)


def test_apply_then_undo_tells_the_crewmate_thread_each_once(world, tmp_path, queued):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        _as_crewmate_thread(c)
        card = await _propose_setting(c)
        assert (await _apply(c, card)).status == 200
        # The same step sent again is a replay: nothing new to say.
        await _apply(c, card)
        card = await _current(c, card["id"])
        undo = card["plan"]["undo"][0]
        r = await c.patch(
            "/api/config/kirocrew", json=undo["body"], headers=_card_headers(card, op="undo")
        )
        assert r.status == 200, await r.text()

    _run(world, store, go)
    assert [key for key, _ in queued] == [SLOT, SLOT]
    assert "the user applied it" in queued[0][1]
    assert "the user undid it" in queued[1][1]


def test_decline_tells_the_crewmate_thread(world, tmp_path, queued):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        _as_crewmate_thread(c)
        card = await _propose_setting(c)
        r = await c.post(
            f"/api/cards/{card['id']}/cancel", json={"revision": card["revision"]}, headers=OWNER
        )
        assert r.status == 200

    _run(world, store, go)
    assert len(queued) == 1 and "declined it" in queued[0][1]


def test_a_failed_apply_names_the_error(world, tmp_path, queued):
    store = CardStore(tmp_path / "c.json")

    async def refuse(_request):
        raise web.HTTPBadRequest(text="nope")

    world.after_write = refuse

    async def go(c):
        _as_crewmate_thread(c)
        card = await _propose_setting(c)
        assert (await _apply(c, card)).status == 400

    _run(world, store, go)
    assert len(queued) == 1 and "pressed Apply and it failed" in queued[0][1]


def test_a_card_from_the_main_assistant_tells_nobody(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    told: list[str] = []
    monkeypatch.setattr(wakes, "schedule_flush", lambda state, key: told.append(key))

    async def go(c):
        card = await _propose_setting(c)
        assert (await _apply(c, card)).status == 200
        other = await _propose_setting(c, value="detailed")
        await c.post(
            f"/api/cards/{other['id']}/cancel", json={"revision": other["revision"]}, headers=OWNER
        )
        return c.server.app["state"]

    state = _run(world, store, go)
    assert told == []
    assert not getattr(state, "_mate_user_actions", {})


def test_an_apply_that_wakes_on_its_own_sends_no_second_note(world, tmp_path, queued):
    store = CardStore(tmp_path / "c.json")

    async def own_wake(request):
        # What the goal, milestones and page-offer routes and _complete's
        # capabilities and tool-access nudges do once they scheduled their wake.
        request[actions.REQ_CARD_OWN_WAKE] = True

    world.after_write = own_wake

    async def go(c):
        _as_crewmate_thread(c)
        card = await _propose_setting(c)
        assert (await _apply(c, card)).status == 200
        world.after_write = None
        card = await _current(c, card["id"])
        undo = card["plan"]["undo"][0]
        await c.patch(
            "/api/config/kirocrew", json=undo["body"], headers=_card_headers(card, op="undo")
        )

    _run(world, store, go)
    # Only the Undo is told: the apply's own wake was its note.
    assert len(queued) == 1 and "the user undid it" in queued[0][1]


@pytest.mark.parametrize(
    ("outcome", "told"),
    [
        (actions.CARD_APPLIED, False),
        (actions.CARD_PARTIAL, False),
        (actions.CARD_UNDONE, False),
        (actions.CARD_DECLINED, True),
        (actions.CARD_APPLY_FAILED, True),
        (actions.CARD_UNDO_FAILED, True),
    ],
)
def test_a_goal_cards_apply_and_undo_stay_silent(queued, outcome, told):
    """rfc-crewmate-goals §3.8: the goal nudge is its apply's wake; Undo and edits say nothing.

    Whatever its plan's steps: a goal with its Heartbeat checked settles on the
    schedules step, which the goal write's own-wake mark never reached.
    """
    mate = _Slot("member-atlas", mate=True)
    rec = {"id": "g1", "kind": "crewmate.goal", "slot_key": mate.key, "title": "Ship"}
    assert actions.note_card(_State(mate), rec, outcome, own_wake=False) is told
    assert len(queued) == int(told)


class _Req(dict):
    def __init__(self, app):
        super().__init__()
        self.app = app


@pytest.mark.parametrize("woke", [True, False])
def test_complete_marks_the_capabilities_wake_as_the_cards_note(monkeypatch, woke):
    from types import SimpleNamespace

    from kiro_crew import change_card_catalog as catalog
    from kiro_crew import crewmate_dynamic_capabilities as dyn
    from kiro_crew.dashboard import goal_setup
    from kiro_crew.dashboard.handlers import change_cards

    store = SimpleNamespace(complete_apply=lambda rec, **kw: None)
    monkeypatch.setattr(change_cards, "card_store_for", lambda state: store)

    async def read_state(*a, **k):
        return {}

    monkeypatch.setattr(change_cards.cards, "read_state", read_state)
    monkeypatch.setattr(change_cards.cards, "after_shows_the_write", lambda *a: True)
    monkeypatch.setattr(catalog, "applied_write_count", lambda *a: 1)
    monkeypatch.setattr(catalog, "build_undo", lambda *a: (None, "x"))
    monkeypatch.setattr(dyn, "enabled_for", lambda member: True)
    monkeypatch.setattr(
        goal_setup, "schedule_capabilities_nudge", lambda s, k, t, *, card_member: woke
    )
    rec = {
        "id": "c1",
        "kind": catalog.KIND_CREWMATE_CAPABILITIES,
        "params": {"member": "pixel"},
        "before": {},
        "evidence": [],
        "plan": {"apply": []},
        "slot_key": "member-pixel",
        "title": "Add jira",
    }
    request = _Req({"state": SimpleNamespace()})
    asyncio.run(change_cards._complete(request, rec, partial=False))
    assert (request.get(actions.REQ_CARD_OWN_WAKE) is True) is woke


# ── the Run in terminal producer ──


def _terminal_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers import mate_terminal_run

    app = web.Application()
    app["state"] = state
    app.router.add_post(
        "/api/chat/slots/{slot}/terminal-run", mate_terminal_run.api_slot_terminal_run
    )
    return app


@pytest.fixture
def owner_gate(monkeypatch):
    from kiro_crew.dashboard.handlers import mate_terminal_run

    async def allow(request, operation):
        return None

    monkeypatch.setattr(mate_terminal_run, "_deny_app_caller", allow)
    monkeypatch.setattr(mate_terminal_run, "require_owner_dashboard_request", allow)


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ({"command": "ls", "outcome": "ran"}, "ran it in a new terminal tab"),
        ({"command": "ls", "outcome": "copied"}, "copied it for them to paste"),
        (
            {"command": "ls", "outcome": "failed", "reason": "no_tab", "source": "run_yourself"},
            "no new terminal tab could be opened",
        ),
    ],
)
def test_run_in_terminal_tells_only_the_owning_crewmate_thread(owner_gate, queued, body, want):
    from aiohttp.test_utils import TestClient, TestServer

    mate, main = _Slot("member-atlas", mate=True), _Slot("chat-main", mate=False)
    state = _State(mate, main)

    async def go():
        client = TestClient(TestServer(_terminal_app(state)))
        await client.start_server()
        try:
            r1 = await client.post("/api/chat/slots/member-atlas/terminal-run", json=body)
            r2 = await client.post("/api/chat/slots/chat-main/terminal-run", json=body)
            return (await r1.json())["noted"], (await r2.json())["noted"]
        finally:
            await client.close()

    assert asyncio.run(go()) == (True, False)
    assert [key for key, _ in queued] == ["member-atlas"]
    assert want in queued[0][1]


def test_run_in_terminal_refuses_an_unknown_outcome(owner_gate, queued):
    from aiohttp.test_utils import TestClient, TestServer

    state = _State(_Slot("member-atlas", mate=True))

    async def go():
        client = TestClient(TestServer(_terminal_app(state)))
        await client.start_server()
        try:
            bad = await client.post(
                "/api/chat/slots/member-atlas/terminal-run", json={"command": "ls", "outcome": "x"}
            )
            empty = await client.post(
                "/api/chat/slots/member-atlas/terminal-run", json={"command": " ", "outcome": "ran"}
            )
            return bad.status, empty.status
        finally:
            await client.close()

    assert asyncio.run(go()) == (400, 400)
    assert queued == []
