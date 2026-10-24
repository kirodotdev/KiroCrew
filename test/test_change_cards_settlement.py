"""Change cards: settling a card, in the conversation and on disk.

Every step and transition is saved before it changes anything or any tab hears of it;
a card is a row of the conversation; a step cut off after its route started is never
replayed; agent-supplied parameters are refused when a redactor would change them; a
read settles a card only once the settlement is saved; housekeeping Undo and poll
admission resume their cards; and crewmate.create cards written by an earlier build
survive a reload.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

import change_cards_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from change_cards_helpers import (
    _FAKE_TOKEN,
    AGENT,
    OWNER,
    SECRET_VALUE,
    SK,
    SLOT,
    Clock,
    World,
    _app,
    _card_headers,
    _propose,
    _propose_setting,
    _reload,
    _run,
)
from crew_log_drain import assert_drained, settle, unsync_appends

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardError, CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.state import _ChatSlot

# The shared fixtures, bound here by name so pytest registers them for this module.
world = change_cards_helpers.world


# ── a step is saved before it changes anything or publishes success ──


def test_a_step_whose_admission_cannot_be_saved_changes_nothing(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    real = cards._write_locked

    def write(path, payload):
        if '"applying"' in payload:
            raise OSError("disk full")
        real(path, payload)

    monkeypatch.setattr(cards, "_write_locked", write)

    async def go(c):
        card = await _propose_setting(c)
        r = await c.patch(
            "/api/config/kirocrew",
            json=card["plan"]["apply"][0]["body"],
            headers=_card_headers(card),
        )
        return r.status, await r.json(), store.get(card["id"])

    status, body, rec = _run(world, store, go)
    assert (status, body["code"]) == (503, cards.CODE_CHECKPOINT_FAILED)
    assert world.calls == [] and world.config["chat.verbosity"] == "standard"
    assert rec["status"] == "pending" and not rec.get("inflight")


def test_a_step_whose_result_cannot_be_saved_never_publishes_success(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    real = cards._write_locked
    state_holder: list[Any] = []

    def write(path, payload):
        # Everything after the admission fails: the result and the review mark.
        if '"applied"' in payload or cards.CODE_CHECKPOINT_FAILED in payload:
            raise OSError("disk full")
        real(path, payload)

    monkeypatch.setattr(cards, "_write_locked", write)

    async def go(c):
        state_holder.append(c.server.app["state"])
        card = await _propose_setting(c)
        r = await c.patch(
            "/api/config/kirocrew",
            json=card["plan"]["apply"][0]["body"],
            headers=_card_headers(card),
        )
        return r.status, await r.json(), store.get(card["id"])

    status, body, rec = _run(world, store, go)
    assert world.config["chat.verbosity"] == "ultra-brief"  # the route did run
    assert (status, body["code"]) == (503, cards.CODE_CHECKPOINT_FAILED)
    assert rec["status"] == "partial" and rec["error"]["code"] == cards.CODE_CHECKPOINT_FAILED
    assert rec["plan"]["undo"] is None
    published = [p["card"]["status"] for k, p in state_holder[0].frames if k == "card_update"]
    assert "applied" not in published and published[-1] == "partial"
    # What is on disk is the admission: a restart reads it as interrupted, not pending.
    loaded = _reload(tmp_path / "c.json").get(rec["id"])
    assert loaded["status"] == "partial" and loaded["error"]["code"] == cards.CODE_INTERRUPTED


# ── a card is part of the conversation ──

SID = "acp-mate-1"


@pytest.fixture
def crew_log(tmp_path, monkeypatch):
    """A crew log for the proposing session, with the emitter on and no writer state.

    The writer is waited out through ``crew_log_drain`` rather than ``emit.flush()``: a
    fixed flush budget is a rate assertion on the host's fsync speed, which a loaded
    Windows runner misses while the writer is still landing entries. The fsync comes out
    of each append because the subject here is what the log records, not that it is
    durable, and the writer is settled while the home pin still holds.
    """
    from types import SimpleNamespace

    from kiro_crew.crew_log import CrewLog
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.dashboard import chat_cards

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    unsync_appends(monkeypatch)
    crew_log_emit.reset_caches()
    chat_cards.reset()
    CrewLog.create(KIND_SESSION, SID, owner="owner", agent="kirocrew", slot=SLOT)
    original = _ChatSlot.__init__

    def _with_client(self, *a, **kw):
        original(self, *a, **kw)
        self._acp_client = SimpleNamespace(session_id=SID)

    monkeypatch.setattr(_ChatSlot, "__init__", _with_client)

    def entries(prefix: str = "") -> list:
        assert_drained()
        handle = CrewLog.open(KIND_SESSION, SID)
        try:
            return [e for e in handle.iter_from(1) if e.type.startswith(prefix)]
        finally:
            del handle

    yield entries
    settle()
    chat_cards.reset()


def _card_rows(app_state) -> list[dict]:
    return [m for m in app_state.get_slot(SLOT).messages if m.get("role") == "card"]


def _run_with_state(world, store, fn):
    """Like ``_run``, also handing the coroutine the app's state."""
    holder: dict[str, Any] = {}

    async def wrapped(c):
        holder["state"] = c.server.app["state"]
        return await fn(c, holder["state"])

    return _run(world, store, wrapped), holder["state"]


def test_a_proposal_is_a_row_of_the_conversation_at_the_point_it_was_proposed(
    world, tmp_path, crew_log
):
    store = CardStore(tmp_path / "c.json")

    async def go(c, state):
        slot = state.get_slot(SLOT)
        slot.append("user", "make my replies shorter", "msg msg-u", broadcast=False)
        slot.append("tool", "🔧 propose_change", "msg msg-tool", broadcast=False)
        card = await _propose_setting(c)
        slot.append("assistant", "I proposed it above.", "msg msg-a", broadcast=False)
        return card

    card, state = _run_with_state(world, store, go)
    roles = [m["role"] for m in state.get_slot(SLOT).messages]
    assert roles == ["user", "tool", "card", "assistant"]
    (row,) = _card_rows(state)
    ref = row["meta"]["card"]
    assert ref == {
        "surface": "change",
        "id": card["id"],
        "slot": SLOT,
        "kind": "setting.change",
        "title": card["title"],
        "status": "pending",
        "risk": card["risk"],
    }
    assert row["content"] == card["title"]
    # The crew log records the same fact, joined to the row by its id.
    (proposed,) = crew_log("card/")
    assert proposed.type == "card/proposed" and proposed.src == "gateway"
    assert proposed.data["card_id"] == card["id"]
    assert proposed.data["slot"] == SLOT and proposed.data["kind"] == "setting.change"
    assert proposed.data["mid"] == row["meta"]["mid"]
    # Names only: never a parameter of the proposal.
    assert "params" not in proposed.data and "ultra-brief" not in json.dumps(proposed.data)


def test_an_outcome_updates_the_row_in_place_and_is_logged_once(world, tmp_path, crew_log):
    store = CardStore(tmp_path / "c.json")

    async def go(c, state):
        card = await _propose_setting(c)
        step = card["plan"]["apply"][0]
        await c.patch("/api/config/kirocrew", json=step["body"], headers=_card_headers(card))
        applied = store.public(store.get(card["id"]))
        row_after_apply = json.loads(json.dumps(_card_rows(state)[0]))
        await c.patch(
            "/api/config/kirocrew",
            json=applied["plan"]["undo"][0]["body"],
            headers=_card_headers(applied, op="undo"),
        )
        # A dismiss re-publishes a finished card: not a second outcome.
        await c.post(f"/api/cards/{card['id']}/dismiss", headers=OWNER)
        return card, row_after_apply

    (card, row_after_apply), state = _run_with_state(world, store, go)
    rows = _card_rows(state)
    assert len(rows) == 1, "an outcome must update the proposal's row, not add one"
    assert row_after_apply["meta"]["card"]["status"] == "applied"
    assert rows[0]["meta"]["card"]["status"] == "undone"
    assert rows[0]["meta"]["mid"] == row_after_apply["meta"]["mid"]
    finished = [(e.data["card_id"], e.data["status"]) for e in crew_log("card/finished")]
    assert finished == [(card["id"], "applied"), (card["id"], "undone")]
    # The open tabs heard each patch for that row.
    patches = [
        p
        for kind, p in getattr(state, "ws_frames", [])
        if kind == "chat_message_update" and p.get("mid") == rows[0]["meta"]["mid"]
    ]
    assert [p["meta"]["card"]["status"] for p in patches] == ["applied", "undone"]


def test_a_cancelled_card_closes_its_row_and_its_log(world, tmp_path, crew_log):
    store = CardStore(tmp_path / "c.json")

    async def go(c, state):
        card = await _propose_setting(c)
        await c.post(f"/api/cards/{card['id']}/cancel", json={"revision": 1}, headers=OWNER)
        return card

    card, state = _run_with_state(world, store, go)
    assert _card_rows(state)[0]["meta"]["card"]["status"] == "cancelled"
    assert [e.data["status"] for e in crew_log("card/finished")] == ["cancelled"]


def test_the_log_is_read_back_from_a_writer_slower_than_the_default_flush_budget(
    world, tmp_path, crew_log, monkeypatch
):
    """The fixture's barrier follows the writer's progress instead of a fixed budget.

    A loaded Windows runner lands the entries but not within ``emit.flush``'s default
    5 s, which failed ``assert emit.flush()`` with the entries still on their way. The
    fsync is slowed here and the default budget cut to nothing, so a barrier that is
    that default fails on any host, and one that waits while the writer lands bytes
    passes.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import store

    class _SlowFsyncOs:
        @staticmethod
        def fsync(fd: int) -> None:
            threading.Event().wait(0.05)  # flake-ok: a slow disk; no assert on its duration

        def __getattr__(self, attr: str) -> Any:
            return getattr(os, attr)

    real_flush = crew_log_emit.flush

    def default_budget_outrun(timeout: float = 5.0) -> bool:
        return real_flush(timeout=0.0 if timeout == 5.0 else timeout)

    monkeypatch.setattr(store, "os", _SlowFsyncOs())
    monkeypatch.setattr(crew_log_emit, "flush", default_budget_outrun)
    card_store = CardStore(tmp_path / "c.json")

    async def go(c, state):
        card = await _propose_setting(c)
        await c.post(f"/api/cards/{card['id']}/cancel", json={"revision": 1}, headers=OWNER)
        return card

    card, _state = _run_with_state(world, card_store, go)
    assert [(e.data["card_id"], e.data["status"]) for e in crew_log("card/finished")] == [
        (card["id"], "cancelled")
    ]


def test_a_typed_secret_never_reaches_the_conversation_or_the_crew_log(world, tmp_path, crew_log):
    store = CardStore(tmp_path / "c.json")

    async def go(c, state):
        r = await c.post(
            "/api/cards/agent/propose",
            json={"kind": "secret.save", "params": {"name": "GH_TOKEN"}},
            headers=AGENT,
        )
        card = await r.json()
        body = dict(card["plan"]["apply"][0]["body"], value=SECRET_VALUE)
        await c.post("/api/secrets", json=body, headers=_card_headers(card))
        return card

    card, state = _run_with_state(world, store, go)
    assert world.secrets["GH_TOKEN"] == SECRET_VALUE
    (row,) = _card_rows(state)
    assert row["meta"]["card"]["status"] == "applied"
    assert SECRET_VALUE not in json.dumps(state.get_slot(SLOT).messages)
    for frame in getattr(state, "ws_frames", []):
        assert SECRET_VALUE not in json.dumps(frame)
    assert crew_log("card/")  # the card was logged...
    from kiro_crew.crew_log import crew_log_dir
    from kiro_crew.crew_log.schema import KIND_SESSION

    for path in crew_log_dir(KIND_SESSION, SID).glob("*.jsonl"):
        assert SECRET_VALUE not in path.read_text(encoding="utf-8")  # ...and never its value


def test_a_card_row_is_display_only_and_never_model_context():
    from kiro_crew.history_projection import DISPLAY_ONLY_ROLES

    assert "card" in DISPLAY_ONLY_ROLES


def test_a_row_outcome_after_a_restart_goes_to_the_slots_live_session(world, tmp_path, crew_log):
    """The proposing session is held in process; once it is forgotten (a gateway
    restart), the slot's live session receives the outcome -- never an id read
    back out of the agent-writable row."""
    from kiro_crew.dashboard import chat_cards

    store = CardStore(tmp_path / "c.json")

    async def go(c, state):
        card = await _propose_setting(c)
        chat_cards.reset()
        # A planted session id in the row's meta must be ignored.
        _card_rows(state)[0]["meta"]["card"]["sid"] = "acp-someone-else"
        await c.post(f"/api/cards/{card['id']}/cancel", json={"revision": 1}, headers=OWNER)
        return card

    card, _state = _run_with_state(world, store, go)
    assert [e.data["card_id"] for e in crew_log("card/finished")] == [card["id"]]


def test_the_emitter_refuses_a_finished_status_the_stores_do_not_have(crew_log):
    from kiro_crew.crew_log import emit as crew_log_emit

    crew_log_emit.on_card_finished(SID, card_id="cc_0a1b2c3d4e5f", status="approved")
    crew_log_emit.on_guide_finished(SID, guide_id="g_0a1b2c3d4e5f", status="applied")
    crew_log_emit.on_card_finished(SID, card_id="cc_0a1b2c3d4e5f", status="applied")
    assert [(e.type, e.data["status"]) for e in crew_log("") if e.type.endswith("/finished")] == [
        ("card/finished", "applied")
    ]


# ── a step cut off after its route started is never replayable ──


def test_a_save_landing_between_the_write_and_the_read_leaves_no_undo(world, tmp_path):
    """Undo's baseline is what the card wrote: another tab's save in between is kept."""
    store = CardStore(tmp_path / "c.json")

    async def another_tab(_request):
        world.config["chat.verbosity"] = "detailed"

    world.after_write = another_tab

    async def go(c):
        card = await _propose_setting(c)
        body = card["plan"]["apply"][0]["body"]
        r = await c.patch("/api/config/kirocrew", json=body, headers=_card_headers(card))
        return r.status, store.get(card["id"])

    status, rec = _run(world, store, go)
    assert status == 200 and rec["status"] == "applied"
    assert rec["plan"]["undo"] is None
    assert rec["undo_unavailable_reason"] == "changed_since_apply"
    assert world.config["chat.verbosity"] == "detailed"


def test_after_shows_the_write_checks_only_the_fields_the_card_set():
    assert cards.after_shows_the_write("setting.change", {"path": "a", "value": 1}, {"value": 1})
    assert not cards.after_shows_the_write(
        "setting.change", {"path": "a", "value": 1}, {"value": 2}
    )
    add = {"path": "a", "op": "add", "item": "x"}
    assert cards.after_shows_the_write("setting.change", add, {"value": ["x", "y"]})
    assert not cards.after_shows_the_write("setting.change", add, {"value": ["y"]})
    upd = {"name": "Radar", "fields": {"description": "d"}}
    after = {"exists": True, "fields": {"description": "d", "prompt": "edited elsewhere"}}
    assert cards.after_shows_the_write("crewmate.update", upd, after)
    assert not cards.after_shows_the_write("crewmate.update", upd, {"fields": {"description": "e"}})
    assert not cards.after_shows_the_write("mcp.toggle", {"name": "gh", "enabled": True}, {})


def _apply_once_then_again(world, store, after_write):
    """Apply once with *after_write* armed in the route, then press Apply again."""
    world.after_write = after_write

    async def go(c):
        card = await _propose_setting(c)
        body = card["plan"]["apply"][0]["body"]
        try:
            first = await c.patch("/api/config/kirocrew", json=body, headers=_card_headers(card))
            first_status = first.status
        except Exception:  # a cancelled handler drops the connection
            first_status = None
        await asyncio.sleep(0)
        world.after_write = None
        again = await c.patch("/api/config/kirocrew", json=body, headers=_card_headers(card))
        frames = [
            p["card"]["status"] for k, p in c.server.app["state"].frames if k == "card_update"
        ]
        return first_status, await again.json(), store.get(card["id"]), frames

    return _run(world, store, go)


def test_a_route_that_raises_after_writing_is_settled_for_review(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def boom(_request):
        raise RuntimeError("wrote, then crashed")

    first, again, rec, frames = _apply_once_then_again(world, store, boom)
    assert first == 500
    assert rec["status"] == "partial" and rec["error"]["code"] == cards.CODE_INTERRUPTED
    assert again["card_replay"] is True  # answered with the record; the route never re-ran
    assert world.calls == [("PATCH", "/api/config/kirocrew")]
    assert frames[-1] == "partial"
    loaded = _reload(tmp_path / "c.json").get(rec["id"])
    assert loaded["status"] == "partial" and loaded["error"]["code"] == cards.CODE_INTERRUPTED


def test_a_route_cancelled_mid_write_is_settled_for_review(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def shutdown(_request):
        raise asyncio.CancelledError()

    _first, again, rec, _frames = _apply_once_then_again(world, store, shutdown)
    assert rec["status"] == "partial" and rec["error"]["code"] == cards.CODE_INTERRUPTED
    assert again["card_replay"] is True
    assert world.calls == [("PATCH", "/api/config/kirocrew")]
    loaded = _reload(tmp_path / "c.json").get(rec["id"])
    assert loaded["status"] == "partial" and not loaded.get("inflight")


def test_a_route_that_raises_its_refusal_fails_the_card_retryably(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def refuse(_request):
        world.config["chat.verbosity"] = "standard"  # a refusal writes nothing
        raise web.HTTPForbidden(text="no")

    first, again, rec, _frames = _apply_once_then_again(world, store, refuse)
    # Raised like a returned 403: recorded as that failure, and Apply may run again.
    assert first == 403
    assert again.get("card_replay") is None and rec["status"] == "applied"
    assert world.config["chat.verbosity"] == "ultra-brief"


def test_a_check_that_raises_before_the_route_leaves_the_step_retryable(
    world, tmp_path, monkeypatch
):
    store = CardStore(tmp_path / "c.json")
    real = routes._before_value
    calls = {"n": 0}

    async def flaky(route, body, match_info):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("read failed before the route ran")
        return await real(route, body, match_info)

    monkeypatch.setattr(routes, "_before_value", flaky)
    first, again, rec, _frames = _apply_once_then_again(world, store, None)
    assert first == 500
    assert again.get("card_replay") is None and rec["status"] == "applied"
    assert world.calls == [("PATCH", "/api/config/kirocrew")]  # only the retry wrote


# ── every other transition is on disk before any tab hears of it ──


def _failing_writes(monkeypatch, marker):
    real = cards._write_locked

    def write(path, payload):
        if marker(payload):
            raise OSError("disk full")
        real(path, payload)

    monkeypatch.setattr(cards, "_write_locked", write)


def test_a_cancel_that_cannot_be_saved_is_refused_and_never_broadcast(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    _failing_writes(monkeypatch, lambda payload: '"cancelled"' in payload)

    async def go(c):
        card = await _propose_setting(c)
        r = await c.post(f"/api/cards/{card['id']}/cancel", json={"revision": 1}, headers=OWNER)
        frames = [
            p["card"]["status"] for k, p in c.server.app["state"].frames if k == "card_update"
        ]
        return r.status, await r.json(), store.get(card["id"]), frames

    status, body, rec, frames = _run(world, store, go)
    assert (status, body["code"]) == (503, cards.CODE_CHECKPOINT_FAILED)
    assert rec["status"] == "pending"  # memory agrees with disk
    assert "cancelled" not in frames
    assert _reload(tmp_path / "c.json").get(rec["id"])["status"] == "pending"


def test_a_preview_that_cannot_be_saved_keeps_the_previous_revision(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    _failing_writes(monkeypatch, lambda payload: '"brief"' in payload)

    async def go(c):
        card = await _propose_setting(c)
        r = await c.post(
            f"/api/cards/{card['id']}/preview",
            json={"revision": 1, "params": {"path": "chat.verbosity", "value": "brief"}},
            headers=OWNER,
        )
        frames = [
            p["card"]["revision"] for k, p in c.server.app["state"].frames if k == "card_update"
        ]
        return r.status, store.get(card["id"]), frames

    status, rec, frames = _run(world, store, go)
    assert status == 503
    assert rec["revision"] == 1 and rec["params"]["value"] == "ultra-brief"
    assert 2 not in frames


def test_a_proposal_that_cannot_be_saved_is_never_shown(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    _failing_writes(monkeypatch, lambda payload: '"ultra-brief"' in payload)

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={
                "kind": "setting.change",
                "params": {"path": "chat.verbosity", "value": "ultra-brief"},
            },
            headers=AGENT,
        )
        return r.status, c.server.app["state"].frames, store.pending(None)

    status, frames, pending = _run(world, store, go)
    assert status == 503
    assert frames == [] and pending == []


# ── agent-supplied parameters are refused when a redactor would change them ──


def test_card_params_carrying_a_credential_or_exfil_link_are_refused():
    ok = {
        "servers": {
            "remote": {"url": "https://mcp.example.com/sse", "headers": {"X-Team": "infra"}},
            "local": {"url": "http://localhost:8080/mcp"},
            "gh": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        },
        "message": "Summarize my inbox every morning",
    }
    cards.check_param_text(ok)  # ordinary values pass
    for bad in (
        {"message": f"use {_FAKE_TOKEN}"},
        {"servers": {"gh": {"command": "npx", "env": {"TOKEN": _FAKE_TOKEN}}}},
        {"servers": {"r": {"url": "https://x.example/h", "headers": {"A": _FAKE_TOKEN}}}},
        {"servers": {"r": {"url": "https://attacker.example/c?d=" + "QUJD" * 40}}},
        {"servers": {"gh": {"command": "npx", "args": ["--key", _FAKE_TOKEN]}}},
        {_FAKE_TOKEN: "x"},
    ):
        with pytest.raises(CardError) as exc:
            cards.check_param_text(bad)
        assert (exc.value.status, exc.value.code) == (400, "invalid_text")


def test_propose_refuses_a_card_whose_params_carry_a_credential(world, tmp_path):
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        r = await c.post(
            "/api/cards/agent/propose",
            json={
                "kind": "mcp.add_custom",
                "params": {"servers": {"gh": {"command": "npx", "env": {"T": _FAKE_TOKEN}}}},
            },
            headers=AGENT,
        )
        return r.status, await r.json(), c.server.app["state"].frames

    status, body, frames = _run(world, store, go)
    assert (status, body["code"]) == (400, "invalid_text")
    assert _FAKE_TOKEN not in json.dumps(body) and frames == []


def test_a_poll_that_finishes_the_card_is_saved_before_any_tab_hears(world, tmp_path, monkeypatch):
    async def read_state(kind, params, evidence, *, state, app):
        return {"known": True, "granted": bool(evidence)}

    monkeypatch.setattr(cards, "read_state", read_state)
    store = CardStore(tmp_path / "c.json")
    params = catalog.validate_params("connection.connect", {"slug": "github"})
    rec = _propose(store, "connection.connect", params, {"known": True, "granted": False})
    store.begin_step(rec, revision=1, op="apply", index=0)
    store.record_success(rec, op="apply", index=0, evidence={"state": "minting"})
    asyncio.run(store.flush(strict=True))
    _failing_writes(monkeypatch, lambda payload: '"applied"' in payload)

    async def mint_poll(_request):
        return web.json_response({"slug": "github", "state": "granted"})

    async def main():
        app = _app(world, store)
        app.router.add_get("/api/connections/mint", mint_poll)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get(
                "/api/connections/mint?slug=github",
                headers=_card_headers({"id": rec["id"], "revision": 1}, step=1),
            )
            frames = [p["card"]["status"] for k, p in app["state"].frames if k == "card_update"]
            return r.status, frames
        finally:
            await client.close()

    status, frames = asyncio.run(main())
    assert status == 503
    assert "applied" not in frames
    # Memory agrees with disk: still applying, the poll free to run again.
    assert rec["status"] == "applying" and not rec.get("inflight")
    assert _reload(tmp_path / "c.json").get(rec["id"])["status"] == "applying"


def _fresh_home(tmp_path, monkeypatch) -> Path:
    """A crew home as a first install has it: no vault, no key, no store yet."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return home


def test_a_fresh_install_proposes_and_saves_a_reminder_with_the_real_key(tmp_path, monkeypatch):
    # The store's own key source (no ``key=`` override) and the real schedule
    # readers: the first proposal births the vault key beside the store, and
    # the records it writes verify when the store is read back.
    import datetime as _dt

    home = _fresh_home(tmp_path, monkeypatch)

    class _Null:
        def log_api_access(self, **_kw):
            return None

    monkeypatch.setattr(routes, "sel", lambda: _Null())
    store = CardStore(home / cards.STORE_FILENAME)
    tomorrow = _dt.datetime(2099, 1, 16, tzinfo=_dt.timezone.utc)
    at = tomorrow.replace(hour=9, minute=0).strftime("%Y-%m-%dT%H:%M")

    async def go(c):
        out = []
        for when in ({"at": at}, {"cron_expr": "0 9 * * *"}):
            params = {"name": "Review open PRs", "message": "Review open PRs", **when}
            r = await c.post(
                "/api/cards/agent/propose",
                json={"kind": "schedule.create", "params": params},
                headers=AGENT,
            )
            out.append((r.status, await r.json()))
        return out

    results = _run(World(), store, go)
    assert [status for status, _ in results] == [200, 200], results
    assert results[0][1]["once"] is True
    again = CardStore(home / cards.STORE_FILENAME)
    assert {c["id"] for c in again.pending(SLOT)} == {b["id"] for _, b in results}


def test_a_fresh_install_proposes_a_setting_change_with_the_real_key(world, tmp_path, monkeypatch):
    home = _fresh_home(tmp_path, monkeypatch)
    store = CardStore(home / cards.STORE_FILENAME)

    async def go(c):
        return await _propose_setting(c)

    card = _run(world, store, go)
    again = CardStore(home / cards.STORE_FILENAME)
    assert [c["id"] for c in again.pending(SLOT)] == [card["id"]]


# ── settlement on a read: persist before publishing, and announce once saved ──


def test_a_read_whose_settlement_cannot_be_saved_publishes_nothing(world, tmp_path, monkeypatch):
    store = CardStore(tmp_path / "c.json")
    real = cards._write_locked
    broken = {"on": False}

    def write(path, payload):
        if broken["on"]:
            raise OSError("disk full")
        real(path, payload)

    monkeypatch.setattr(cards, "_write_locked", write)

    async def go(c, state):
        card = await _propose_setting(c)
        del state._slots[SLOT]
        state.open_slots_restored = True
        from kiro_crew.dashboard.channel_slots import note_slot_closed

        note_slot_closed(state, SLOT)
        broken["on"] = True
        failed = await c.get("/api/cards/pending", headers=OWNER)
        failed_body = await failed.json()
        held = dict(store.get(card["id"]))
        on_disk = CardStore(tmp_path / "c.json").get(card["id"])["status"]
        frames_while_broken = list(state.frames)
        broken["on"] = False
        ok = await c.get("/api/cards/pending", headers=OWNER)
        return card, failed.status, failed_body, held, on_disk, frames_while_broken, await ok.json()

    (card, status, body, held, on_disk, frames_while_broken, after), state = _run_with_state(
        world, store, go
    )
    # Disk still holds the card pending, so neither the answer nor memory says otherwise.
    assert (status, body["code"]) == (503, cards.CODE_CHECKPOINT_FAILED)
    assert held["status"] == "pending" and on_disk == "pending"
    assert not any(f[1]["card"]["status"] == "cancelled" for f in frames_while_broken)
    # The next settlement that saves derives the transition again and announces it.
    assert [c["status"] for c in after["cards"] if c["id"] == card["id"]] == ["cancelled"]
    cancelled = [f for f in state.frames if f[1]["card"]["status"] == "cancelled"]
    assert len(cancelled) == 1
    assert CardStore(tmp_path / "c.json").get(card["id"])["status"] == "cancelled"


def test_undoing_housekeeping_restores_pruned_cards_in_their_place():
    clock = Clock()
    store = CardStore(None, clock=clock)
    first = _propose(store)
    store.cancel(first, first["revision"])
    second = _propose(store, params={"path": "chat.verbosity", "value": "detailed"})
    clock.now += cards.FINISHED_RETAIN_SECONDS + 1  # first is pruned, second expires
    changed, undo = store.housekeep(lambda _key: True)
    assert [c["id"] for c in changed] == [second["id"]]
    assert first["id"] not in [c["id"] for c in store.pending(None)]
    store.undo_housekeeping(undo)
    assert list(store._cards) == [first["id"], second["id"]]
    assert store.get(first["id"])["status"] == "cancelled"
    assert second["status"] == "pending"  # restored in place, the same object


def test_undoing_housekeeping_restores_a_failed_card_it_expired():
    # ``failed`` is a finished status, yet still retryable: ``_refresh`` expires
    # it, so its snapshot must be taken by value for the rollback to see it.
    clock = Clock()
    store = CardStore(None, clock=clock)
    rec = _propose(store)
    store.begin_step(rec, revision=1, op="apply", index=0)
    store.record_failure(rec, op="apply", index=0, status=400, body={"error": "no", "code": "x"})
    assert rec["status"] == "failed"
    clock.now = rec["expires_at"] + 1
    changed, undo = store.housekeep(lambda _key: True)
    assert [(c["id"], c["status"]) for c in changed] == [(rec["id"], "expired")]
    store.undo_housekeeping(undo)
    assert rec["status"] == "failed" and rec["error"]["code"] == "x"


def test_a_write_that_never_reports_back_goes_to_review_not_back_to_pending():
    clock = Clock()
    store = CardStore(None, clock=clock)
    rec = _propose(store)
    store.begin_step(rec, revision=rec["revision"], op="apply", index="0")  # a PATCH
    clock.now += cards.INFLIGHT_STALE_SECONDS + 1
    store.sweep()
    assert rec["status"] == "partial" and not rec.get("inflight")
    assert rec["error"]["code"] == cards.CODE_INTERRUPTED
    # Terminal: a repeat of the step is answered with the record, never run again.
    again = store.begin_step(rec, revision=rec["revision"], op="apply", index="0")
    assert again["replay"] is True


def test_a_stale_poll_is_admitted_again():
    clock = Clock()
    store = CardStore(None, clock=clock)
    rec = _propose(store)
    rec["plan"]["apply"][0] = {**rec["plan"]["apply"][0], "method": "GET", "repeat": True}
    rec["status"] = "applying"
    rec["inflight"] = {"op": "apply", "step": 0, "started_at": clock.now}
    clock.now += cards.INFLIGHT_STALE_SECONDS + 1
    store.sweep()
    assert rec["status"] == "applying" and rec["inflight"] is None


def test_a_one_shot_card_carries_the_wall_time_and_its_instant_in_the_card_zone():
    from datetime import datetime, timezone

    la = "America/Los_Angeles"
    params = catalog.validate_params(
        "schedule.create",
        {
            "name": "Review open PRs",
            "message": "Review open PRs",
            "at": "2099-10-07T09:00",
            "timezone": la,
        },
    )
    context = cards.one_shot_context(params["at"], la)
    nine_am_la = datetime(2099, 10, 7, 16, 0, tzinfo=timezone.utc).timestamp()
    assert context["at_ts"] == context["next_run_at"] == nine_am_la
    preview = catalog.build_preview("schedule.create", params, {}, context)
    row = next(c for c in preview["changes"] if c.get("field") == "at")
    # The row is the wall time the person asked for, named with its zone; the
    # client reads it in that zone (cards/oneShot.ts), never in its own.
    assert (row["after"], row["timezone"]) == ("2099-10-07T09:00", la)
    assert preview["apply"][0]["body"]["at"] == int(nine_am_la)


def _legacy_create_card(
    store: CardStore, *, applied: bool, apply: list[dict] | None = None
) -> dict:
    """A crewmate.create card as a build without goal records stored it."""
    params = catalog.validate_params(
        "crewmate.create",
        {"name": "scout", "goal": "Watch the queue", "schedule": {"cron_expr": "0 9 * * 1"}},
    )
    before = {"exists": False, "inherited_auto_approve": []}
    preview = catalog.build_preview("crewmate.create", params, before, {})
    preview["apply"] = apply if apply is not None else catalog.legacy_create_apply(params)
    rec = store.propose(
        slot_key=SLOT,
        session_key=SK,
        kind="crewmate.create",
        params=params,
        reason="",
        preview=preview,
        before=before,
        context={},
    )
    if applied:
        evidence = [{"member_id": "scout", "name": "scout"}, {"id": "job1"}]
        for i, ev in enumerate(evidence):
            store.begin_step(rec, revision=rec["revision"], op="apply", index=i)
            store.record_success(rec, op="apply", index=i, evidence=ev)
        undo, reason = catalog.build_undo("crewmate.create", params, before, evidence, 2, {})
        store.complete_apply(rec, after={"exists": True}, undo=undo, undo_reason=reason)
    return json.loads(json.dumps(rec))


def _base_build_create_apply(*, first_greeting: bool) -> list[dict]:
    """The apply plan the released builds wrote for ``_legacy_create_card``'s params.

    Spelled out literally, not taken from ``legacy_create_apply``, so a drift in
    that rebuild cannot hide behind itself.
    """
    agent = {
        "name": "scout",
        "kiro_agent": catalog.DEFAULT_CREWMATE_TEMPLATE,
        "description": "Watch the queue",
        "source": "kirocrew",
    }
    if first_greeting:
        agent["first_greeting"] = True
    fill = {"field": "member_id", "source": "step", "step": 0, "key": "member_id"}
    schedule = {
        "name": "scout schedule",
        "message": "Watch the queue",
        "agent": catalog.DEFAULT_CREWMATE_TEMPLATE,
        "silent": False,
        "hide_in_chat": False,
        "strict_schedule": True,
        "cron": "0 9 * * 1",
        "member_id": catalog.placeholder(fill),
    }
    return [
        {"method": "POST", "path": "/api/agents", "body": agent},
        {"method": "POST", "path": "/api/crons", "body": schedule, "fill": [fill]},
    ]


@pytest.mark.parametrize("first_greeting", [True, False], ids=["with-greeting", "pre-greeting"])
@pytest.mark.parametrize("applied", [True, False], ids=["applied", "proposed"])
def test_a_base_build_create_card_survives_a_store_reload(tmp_path, first_greeting, applied):
    path = tmp_path / "change_cards.json"
    store = CardStore(path, clock=Clock(), key=lambda: b"k" * 32)
    apply = _base_build_create_apply(first_greeting=first_greeting)
    rec = _legacy_create_card(store, applied=applied, apply=apply)
    asyncio.run(store.flush())
    again = CardStore(path, clock=Clock(), key=lambda: b"k" * 32)
    asyncio.run(again.warm())
    loaded = again.get(rec["id"])
    assert loaded is not None
    if applied:
        assert loaded["plan"]["apply"] == apply
        assert [s["path"] for s in loaded["plan"]["undo"]] == [
            "/api/crons/job1",
            "/api/agents/scout",
        ]
    else:
        assert loaded["status"] == rec["status"]


def test_a_legacy_applied_create_card_survives_a_rebuild_and_keeps_its_undo():
    """Stored before goal records: schedule at step 1, goal in description."""
    rec = _legacy_create_card(CardStore(None, clock=Clock()), applied=True)
    rebuilt = cards.rebuild_record(rec)
    assert rebuilt is not None
    assert rebuilt["plan"]["apply"] == rec["plan"]["apply"]
    assert [s["path"] for s in rebuilt["plan"]["undo"]] == ["/api/crons/job1", "/api/agents/scout"]


def test_a_legacy_unrun_create_card_runs_the_current_plan():
    rec = _legacy_create_card(CardStore(None, clock=Clock()), applied=False)
    rebuilt = cards.rebuild_record(rec)
    assert rebuilt is not None
    assert [s["path"].rsplit("/", 1)[-1] for s in rebuilt["plan"]["apply"]] == [
        "agents",
        "long-term",
        "crons",
    ]


def test_a_current_create_cards_undo_finds_its_schedule_at_step_two():
    p = catalog.validate_params(
        "crewmate.create",
        {"name": "scout", "goal": "Watch", "schedule": {"cron_expr": "0 9 * * 1"}},
    )
    undo, _ = catalog.build_undo(
        "crewmate.create", p, {}, [{"name": "scout"}, {}, {"id": "j2"}], applied_steps=3
    )
    assert [s["path"] for s in undo] == ["/api/crons/j2", "/api/agents/scout"]


@pytest.mark.parametrize(
    "evidence, job_id",
    [
        ([{"member_id": "scout", "name": "scout"}, {"id": "job1"}], "job1"),
        ([{"member_id": "scout", "name": "scout"}, {}, {"id": "job2"}], "job2"),
    ],
    ids=["legacy", "current"],
)
def test_a_create_cards_after_read_finds_its_schedule_at_its_own_step(
    monkeypatch, evidence, job_id
):
    """A legacy card's schedule is step 1, the current one's step 2: both are read."""
    from types import SimpleNamespace

    monkeypatch.setattr(cards, "_sync_read_state", lambda *_a: {"exists": True})
    asked: list[str] = []

    async def get_job_async(jid: str):
        asked.append(jid)
        return SimpleNamespace() if jid == job_id else None

    state = SimpleNamespace(crons=SimpleNamespace(get_job_async=get_job_async))
    p = catalog.validate_params(
        "crewmate.create",
        {"name": "scout", "goal": "Watch", "schedule": {"cron_expr": "0 9 * * 1"}},
    )
    got = asyncio.run(cards.read_state("crewmate.create", p, evidence, state=state, app=None))
    assert asked == [job_id]
    assert got["schedule_exists"] is True


def test_a_dismissed_card_tells_the_crewmate_feed(world, tmp_path, monkeypatch):
    """A dismiss keeps the card's status, so it writes no log entry: the route tells a
    crewmate page's waiting list itself, or a dismissed failed card stays listed."""
    from kiro_crew.dashboard import crewmate_feed

    told: list[str] = []
    monkeypatch.setattr(crewmate_feed, "question_retired", told.append)
    store = CardStore(tmp_path / "c.json")

    async def go(c):
        card = await _propose_setting(c)
        told.clear()
        step = card["plan"]["apply"][0]
        await c.patch("/api/config/kirocrew", json=step["body"], headers=_card_headers(card))
        told.clear()
        r = await c.post(f"/api/cards/{card['id']}/dismiss", headers=OWNER)
        return r.status

    assert _run(world, store, go) == 200
    assert told == [SLOT]
